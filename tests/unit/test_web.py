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
from pathlib import Path

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
    _host_pending_entries,
    _host_timeline,
    _search_packages,
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
    older = _state(
        fleet_lxc_data=[dict(node="pve-01", name="sonarr", id="101", app="Updated: v4.0 → v4.1", os="OK")],
        fleet_changed=True,
    )
    write_history(
        older,
        history_dir=history_dir,
        keep=0,
        timestamp="20260101T000000000000Z",
        briefing=briefing.prepare_body(older),
    )
    newer = _state(
        fleet_lxc_data=[dict(node="pve-01", name="sonarr", id="101", app="FAILED + ROLLED BACK", os="OK")],
        fleet_error_log=[dict(host="sonarr", task="app update", error="boom")],
        fleet_warning_log=[dict(host="sonarr", task="snapshot", warning="snapshot slow")],
        fleet_changed=True,
        fleet_failed=True,
    )
    write_history(
        newer,
        history_dir=history_dir,
        keep=0,
        timestamp="20260102T000000000000Z",
        briefing=briefing.prepare_body(newer),
    )
    scan_mod.write_pending(
        {
            "timestamp": "20260103T000000000000Z",
            "hosts": {
                "web-01": {
                    "kind": "remote",
                    "pkg_mgr": "apt",
                    "pending_count": 2,
                    "pending": ["curl", "openssl"],
                    "error": None,
                }
            },
            "lxc": {
                "101": {
                    "node": "pve-01",
                    "name": "sonarr",
                    "skipped": None,
                    "os_pending_count": 1,
                    "os_pending": ["libssl3"],
                    "app": {"script": "sonarr", "current": "4.0.17", "latest": "4.0.18", "outdated": True},
                    "error": None,
                }
            },
        },
        history_dir=history_dir,
        keep=0,
    )


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
    args = build_run_args({"scan": "on", "dry_run": "on", "force_notify": "on", "phases": "lxc", "limit": "105"})
    assert args == ["--scan", "--limit", "105"]


def test_build_args_rejects_shell_metacharacters():
    with pytest.raises(ValueError, match="invalid token"):
        build_run_args({"limit": "pve-01;rm -rf /"})


def test_build_args_rejects_unknown_phase():
    with pytest.raises(ValueError, match="unknown phase"):
        build_run_args({"phases": "lxc,bogus"})


def test_build_args_limit_accepts_qualified_cluster_id():
    args = build_run_args({"limit": "alpha/101"})
    assert args == ["--limit", "alpha/101"]


def test_build_args_limit_rejects_too_many_slashes():
    with pytest.raises(ValueError, match="invalid token"):
        build_run_args({"limit": "a/b/c"})


def test_build_args_limit_rejects_shell_metacharacters_in_qualified_token():
    with pytest.raises(ValueError, match="invalid token"):
        build_run_args({"limit": "alpha/101;rm -rf /"})


def test_build_args_phases_rejects_qualified_token():
    # cluster/id qualification is a --limit-only concept — phases stay bare.
    with pytest.raises(ValueError, match="invalid token"):
        build_run_args({"phases": "alpha/lxc"})


# --- read-only pages --------------------------------------------------------- #


def test_index_shows_latest_run_and_pending(history_dir):
    resp = _client(history_dir).get("/")
    assert resp.status_code == 200
    assert "20260102T000000000000Z" in resp.text  # latest run
    assert "20260103T000000000000Z" in resp.text  # latest pending scan
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
    assert eps == {"LXCs": 2, "VMs": 1, "PVE hosts": 2, "remote hosts": 1, "custom systems": 0}


def test_endpoint_counts_missing_inventory_and_scan(tmp_path):
    eps = _endpoint_counts(str(tmp_path / "nope.ini"), None)
    assert {e["label"] for e in eps} == {"LXCs", "VMs", "PVE hosts", "remote hosts", "custom systems"}
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
    # all four series render server-side (no-JS fallback shows everything).
    # Normalize formatter-inserted whitespace between HTML attributes.
    compact = " ".join(resp.text.split())
    for cls in ("s-os", "s-app", "s-err", "s-warn"):
        assert f'<polyline class="{cls}"' in compact
    # legend entries are show/hide toggles, all pressed by default
    for key in ("os", "app", "err", "warn"):
        assert f'data-series="{key}" aria-pressed="true"' in compact
    # per-run warning counts flow into the hover-layer data island
    island = resp.text.split('id="trend-data">', 1)[1].split("</script>", 1)[0]
    runs = json.loads(island)["runs"]
    assert [r["warn"] for r in runs] == [0, 1]
    assert [r["err"] for r in runs] == [0, 1]
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
    assert "errors +1" in resp.text  # newer run vs older run


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


def test_pending_page_legacy_bare_id_key_shows_id(history_dir):
    """Old persisted snapshots (bare-id lxc keys, no `id` field) still render."""
    resp = _client(history_dir).get("/pending")
    assert '<td class="num">101</td>' in resp.text


def test_pending_page_node_qualified_keys(tmp_path):
    """New snapshots key lxc entries by node/id — two clusters' 101 both render."""
    d = tmp_path / "history"
    _seed_history(d)
    scan_mod.write_pending(
        {
            "timestamp": "20260104T000000000000Z",
            "hosts": {},
            "lxc": {
                "alpha-01/101": {
                    "node": "alpha-01",
                    "id": "101",
                    "name": "sonarr",
                    "skipped": None,
                    "os_pending_count": 1,
                    "os_pending": ["libssl3"],
                    "app": None,
                    "error": None,
                },
                "beta-01/101": {
                    "node": "beta-01",
                    "id": "101",
                    "name": "radarr",
                    "skipped": None,
                    "os_pending_count": 0,
                    "os_pending": [],
                    "app": None,
                    "error": None,
                },
            },
        },
        history_dir=d,
        keep=0,
    )
    resp = _client(d).get("/pending")
    assert resp.status_code == 200
    assert "sonarr" in resp.text and "radarr" in resp.text
    assert "alpha-01" in resp.text and "beta-01" in resp.text
    # The ID column itself still shows the bare container id, not the node/id
    # key — it appears twice, once per cluster's "101".
    assert resp.text.count('<td class="num">101</td>') == 2
    # But each row's link now points at the composite node/id path (Task 5)
    # so the two clusters' identical ids don't collide on one host page.
    assert 'href="/hosts/alpha-01/101"' in resp.text
    assert 'href="/hosts/beta-01/101"' in resp.text


def test_pending_unknown_ref_404(history_dir):
    assert _client(history_dir).get("/pending", params={"ref": "nope"}).status_code == 404


def test_host_drilldown_across_runs(history_dir):
    resp = _client(history_dir).get("/hosts/sonarr")
    assert resp.status_code == 200
    assert "Updated: v4.0 → v4.1" in resp.text  # older run record
    assert "FAILED + ROLLED BACK" in resp.text  # newer run record
    assert "boom" in resp.text  # error record matches host


def test_host_drilldown_unknown_host_is_empty(history_dir):
    resp = _client(history_dir).get("/hosts/ghost")
    assert resp.status_code == 200
    assert "No records" in resp.text


# --- package detail disclosure (PR1) ------------------------------------------ #


def test_run_detail_renders_package_disclosure(history_dir):
    """PR1: a record carrying exact OS packages renders an expandable
    <details class="pkg-list"> list on the run-detail page — not a raw blob."""
    state = _state(
        fleet_vm_data=[
            dict(
                node="pve-01",
                vmid="200",
                name="my-vm",
                status="UPDATED",
                pkg_count=2,
                packages=[
                    {"name": "curl", "from": "7.1", "to": "7.2"},
                    {"name": "openssl", "from": "1.1", "to": "1.1.2"},
                ],
            )
        ],
        fleet_changed=True,
    )
    write_history(
        state,
        history_dir=history_dir,
        keep=0,
        timestamp="20260110T000000000000Z",
        briefing=briefing.prepare_body(state),
    )
    resp = _client(history_dir).get("/history/20260110T000000000000Z")
    assert resp.status_code == 200
    assert '<details class="pkg-list">' in resp.text
    assert "<summary>2 pkgs</summary>" in resp.text
    assert '<span class="mono">curl</span> 7.1 → 7.2' in resp.text
    assert '<span class="mono">openssl</span> 1.1 → 1.1.2' in resp.text


def test_host_page_renders_package_disclosure(history_dir):
    """PR1: the host drill-down shows the same <details> disclosure — and only
    for the record that carries packages (legacy sibling records add none).
    PR4: the fixture's pending snapshot also renders one (its sonarr entry
    carries os_pending names), so the record's disclosure is one of two."""
    state = _state(
        fleet_lxc_data=[
            dict(
                node="pve-01",
                name="sonarr",
                id="101",
                app="Updated: v4.0 → v4.1",
                os="OK",
                snap=True,
                packages=[{"name": "libssl3", "from": "1.0", "to": "1.1"}],
            )
        ],
        fleet_changed=True,
    )
    write_history(
        state,
        history_dir=history_dir,
        keep=0,
        timestamp="20260111T000000000000Z",
        briefing=briefing.prepare_body(state),
    )
    resp = _client(history_dir).get("/hosts/sonarr")
    assert resp.status_code == 200
    # one record disclosure + one pending-snapshot disclosure (fixture)
    assert resp.text.count('<details class="pkg-list">') == 2
    assert "<summary>1 pkgs</summary>" in resp.text
    assert '<span class="mono">libssl3</span> 1.0 → 1.1' in resp.text


def test_legacy_records_render_no_empty_package_disclosure(history_dir):
    """PR1: pre-PR1 records (no `packages` key) render no disclosure — the run
    page shows a muted placeholder cell, the host page nothing at all. No
    phantom '0 pkgs' <details> may appear."""
    # the fixture seeds legacy-shape records (no packages key on any run)
    resp = _client(history_dir).get("/history/20260101T000000000000Z")
    assert resp.status_code == 200
    assert '<details class="pkg-list">' not in resp.text
    assert "pkgs" not in resp.text
    assert '<span class="muted">—</span>' in resp.text  # the packages cell placeholder

    host = _client(history_dir).get("/hosts/sonarr")
    assert host.status_code == 200
    # PR4: the fixture's pending snapshot (sonarr, os_pending=["libssl3"]) adds
    # its own disclosure — but the legacy run records add none, and no phantom
    # empty (<details>0 pkgs) disclosure may ever appear.
    assert ">1 pkg(s)</summary>" in host.text
    assert "libssl3" in host.text
    assert "0 pkgs" not in host.text
    assert "pkgs" not in host.text


def test_host_drilldown_composite_node_id_routes_and_filters(tmp_path):
    """/hosts/{node}/{id} (Task 5) must route via the :path converter and
    filter to that (node, id) pair only — a same-id container on a different
    node (the whole point of multi-cluster qualification) must not bleed in."""
    d = tmp_path / "history"
    state = _state(
        fleet_lxc_data=[
            dict(node="pve-01", name="sonarr", id="101", app="SONARR_ONLY_v4_1", os="OK"),
            dict(node="pve-02", name="radarr", id="101", app="RADARR_ONLY_v5_2", os="OK"),
        ],
        fleet_error_log=[dict(host="pve-01/101", task="app update", error="pve-01 boom")],
        fleet_warning_log=[dict(host="pve-02/101", task="disk space", warning="pve-02 warn")],
        fleet_changed=True,
        fleet_failed=True,
    )
    write_history(
        state, history_dir=d, keep=0, timestamp="20260105T000000000000Z", briefing=briefing.prepare_body(state)
    )

    # Exclusion assertions use record-table-only markers (app value, warning /
    # error text) — bare host *names* also appear in the page's global search
    # index nav, so "radarr"/"sonarr" would leak in regardless of filtering.
    resp = _client(d).get("/hosts/pve-01/101")
    assert resp.status_code == 200
    assert "SONARR_ONLY_v4_1" in resp.text  # pve-01/101's own record
    assert "pve-01 boom" in resp.text  # pve-01/101's own error
    assert "RADARR_ONLY_v5_2" not in resp.text  # pve-02/101's record excluded
    assert "pve-02 warn" not in resp.text  # pve-02/101's warning excluded

    resp2 = _client(d).get("/hosts/pve-02/101")
    assert resp2.status_code == 200
    assert "RADARR_ONLY_v5_2" in resp2.text
    assert "pve-02 warn" in resp2.text
    assert "SONARR_ONLY_v4_1" not in resp2.text
    assert "pve-01 boom" not in resp2.text


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
        return {"id": run_id, "argv": [], "args": [], "started": run_id, "pid": 0, "finished": run_id, "rc": 0}


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
    (mgr.runs_dir / "ORPHan.json").write_text(
        json.dumps(
            {
                "id": "ORPHan",
                "argv": [],
                "args": [],
                "started": "T",
                "pid": 2**22 + 12345,
                "finished": None,
                "rc": None,
            }
        ),
        encoding="utf-8",
    )
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
    mgr = RunManager(tmp_path / "runs-home", command=[sys.executable, "-c", "print('fleet says hello')"])
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
    client = _client(history_dir)  # default RunManager over the history dir
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
    settings = GlobalSettings(
        fleet_history_dir=str(project / "history"), host_vars_dir=str(project / "host_vars"), **kw
    )
    app = create_app(settings, vars_path=str(project / "vars.yml"), inventory_path=str(project / "hosts.ini"))
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
    assert client.post("/inventory/add", data={"group": "remote_hosts", "name": "x"}).status_code == 401
    assert client.post("/inventory/remove", data={}).status_code == 401
    assert client.post("/settings", data={}).status_code == 401
    assert client.post("/settings/raw", data={}).status_code == 401
    assert client.post("/ssh/push", data={}).status_code == 401


def test_enroll_vm_writes_ini_and_vars(project):
    client = _project_client(project)
    resp = client.post(
        "/inventory/add",
        data={
            "group": "proxmox_vms",
            "name": "media-vm",
            "ansible_host": "10.0.0.50",
            "vmid": "200",
            "pve_node": "pve-01",
            "canary": "true",
            "kuma_id": "7",
        },
        follow_redirects=False,
    )
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
    resp = client.post(
        "/inventory/add",
        data={
            "group": "remote_hosts",
            "name": "web-01",
            "ansible_host": "10.0.0.5",
            "pre_update_cmd": "systemctl stop app",
            "mw_days": "Sat,Sun",
            "mw_start": "02:00",
            "mw_end": "05:00",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    import yaml as _yaml

    hv = _yaml.safe_load((project / "host_vars" / "web-01.yml").read_text())
    assert hv["pre_update_cmd"] == "systemctl stop app"
    assert hv["maintenance_window"] == {"days": ["Sat", "Sun"], "start": "02:00", "end": "05:00"}


def test_enroll_validation_errors_400(project):
    client = _project_client(project)
    # custom host without custom_config
    assert (
        client.post(
            "/inventory/add",
            data={
                "group": "custom_hosts",
                "name": "db-01",
                "ansible_host": "10.0.0.9",
            },
        ).status_code
        == 400
    )
    # vm without vmid
    assert (
        client.post(
            "/inventory/add",
            data={
                "group": "proxmox_vms",
                "name": "v",
                "ansible_host": "10.0.0.9",
                "pve_node": "pve-01",
            },
        ).status_code
        == 400
    )
    # bad maintenance window key time format is accepted as str, but bad day
    # set is not validated here — bad host name is:
    assert (
        client.post(
            "/inventory/add",
            data={
                "group": "remote_hosts",
                "name": "bad name",
                "ansible_host": "10.0.0.9",
            },
        ).status_code
        == 400
    )
    # duplicate
    assert (
        client.post(
            "/inventory/add",
            data={
                "group": "proxmox_nodes",
                "name": "pve-01",
                "ansible_host": "10.0.0.9",
            },
        ).status_code
        == 400
    )


def test_remove_with_cleanup(project):
    client = _project_client(project)
    client.post(
        "/inventory/add",
        data={
            "group": "proxmox_vms",
            "name": "media-vm",
            "ansible_host": "10.0.0.50",
            "vmid": "200",
            "pve_node": "pve-01",
            "kuma_id": "7",
            "mw_days": "Sat",
        },
        follow_redirects=False,
    )
    resp = client.post(
        "/inventory/remove",
        data={
            "group": "proxmox_vms",
            "name": "media-vm",
            "cleanup": "true",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "media-vm" not in (project / "hosts.ini").read_text()
    import yaml as _yaml

    data = _yaml.safe_load((project / "vars.yml").read_text())
    assert data.get("vm_kuma_map", {}) == {}
    assert not (project / "host_vars" / "media-vm.yml").exists()


def test_remove_missing_400(project):
    assert (
        _project_client(project).post("/inventory/remove", data={"group": "remote_hosts", "name": "ghost"}).status_code
        == 400
    )


def test_inventory_bootstrap_when_missing(project):
    (project / "hosts.ini").unlink()
    client = _project_client(project)
    page = client.get("/inventory").text
    assert "Create hosts.ini" in page
    assert client.post("/inventory/create", data={}, follow_redirects=False).status_code == 303
    assert "[proxmox_vms]" in (project / "hosts.ini").read_text()


def test_settings_page_and_diffed_save(project):
    client = _project_client(project)
    page = client.get("/settings").text
    assert "lxc_forks" in page and "discord_webhook" in page
    resp = client.post(
        "/settings",
        data={
            "lxc_forks": "5",  # changed
            "vm_forks": "2",  # unchanged (default)
            "lxc_dry_run": "false",  # unchanged
            "discord_webhook": "",  # secret left blank → keep
            "canary_hosts": "105\nmedia-vm",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "lxc_forks" in resp.headers["location"]
    assert "vm_forks" not in resp.headers["location"]
    import yaml as _yaml

    data = _yaml.safe_load((project / "vars.yml").read_text())
    assert data["lxc_forks"] == 5
    assert data["canary_hosts"] == ["105", "media-vm"]
    assert "vm_forks" not in data  # untouched fields never written
    assert "discord_webhook" not in data  # blank secret kept
    text = (project / "vars.yml").read_text()
    assert "# hand-written comment that must survive" in text


def test_settings_invalid_value_400(project):
    assert _project_client(project).post("/settings", data={"lxc_forks": "banana"}).status_code == 400


def test_settings_raw_replace_and_reject(project):
    client = _project_client(project)
    assert client.post("/settings/raw", data={"content": "key: [unclosed"}).status_code == 400
    assert client.post("/settings/raw", data={"content": "lxc_forks: nope"}).status_code == 400
    resp = client.post("/settings/raw", data={"content": "# replaced\nlxc_forks: 9\n"}, follow_redirects=False)
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
    ok, out = push_key("10.0.0.5", "root", "s3cret!", port=2222, pubkey_path=str(pub), run=fake)
    assert ok and out == "ok\n"
    argv, kw = fake.calls[0]
    assert argv[0] == "ssh-copy-id"
    assert "root@10.0.0.5" in argv and "2222" in argv
    assert all("s3cret!" not in a for a in argv)  # never on the cmdline
    assert kw["env"]["FLEET_SSH_PW"] == "s3cret!"  # only in the env
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
    monkeypatch.setattr(sshsetup, "push_key", lambda *a, **kw: (True, "key installed"))
    monkeypatch.setattr(sshsetup, "test_key", lambda *a, **kw: (False, "Permission denied"))
    client = _project_client(project)
    resp = client.post("/ssh/push", data={"host": "10.0.0.5", "user": "root", "password": "pw"})
    assert resp.status_code == 200
    assert "key installed" in resp.text and "push succeeded" in resp.text
    resp = client.post("/ssh/test", data={"host": "10.0.0.5"})
    assert resp.status_code == 200
    assert "test failed" in resp.text and "Permission denied" in resp.text


def test_ssh_push_invalid_host_400(project):
    assert _project_client(project).post("/ssh/push", data={"host": "bad;host", "password": "pw"}).status_code == 400


# --- template helpers (filters + flare) ---------------------------------------- #


def test_ts_human_and_iso():
    assert ts_human("20260102T153045123456Z") == "2026-01-02 15:30 UTC"
    assert ts_iso("20260102T153045123456Z") == "2026-01-02T15:30:45Z"
    # malformed input falls back loudly-visible but never raises
    assert ts_human("not-a-ts") == "not-a-ts"
    assert ts_iso("not-a-ts") == ""


def test_ts_span_wraps_for_client_localization():
    html = str(ts_span("20260102T153045123456Z"))
    assert html == ('<span class="ts" data-utc="2026-01-02T15:30:45Z">2026-01-02 15:30 UTC</span>')
    # malformed input degrades to plain escaped text, no span
    assert str(ts_span("<not-a-ts>")) == "&lt;not-a-ts&gt;"


def test_spark_points():
    pts = spark_points([0, 5, 10], 100, 20)
    pairs = [tuple(float(n) for n in p.split(",")) for p in pts.split()]
    assert len(pairs) == 3
    assert pairs[0][0] < pairs[1][0] < pairs[2][0]  # x advances
    assert pairs[0][1] > pairs[2][1]  # larger value plots higher
    assert spark_points([]) == ""
    assert len(spark_points([7]).split()) == 2  # single value → flat line
    assert spark_points(["a", "b"]) == ""  # non-numeric → empty


def test_health_score():
    assert _health_score(None, None) == 100
    failed = {"failed": True, "counts": {"errors": 2, "warnings": 1}}
    assert _health_score(failed, None) == 100 - 25 - 10 - 2
    assert _health_score(None, {"outdated_apps": 3}) == 97
    floor = {"failed": True, "counts": {"errors": 50, "warnings": 0}}
    assert _health_score(floor, None) == 0  # clamped


def test_activity_weeks_grid_shape_and_levels():
    from datetime import date

    rows = [
        {"timestamp": "20260601T000000000000Z", "failed": False},
        {"timestamp": "20260601T120000000000Z", "failed": True},
        {"timestamp": "20260603T000000000000Z", "failed": False},
        {"timestamp": "bogus", "failed": False},  # ignored, never raises
    ]
    grid = _activity_weeks(rows, weeks=4, today=date(2026, 6, 11))
    assert len(grid) == 4 and all(len(week) == 7 for week in grid)
    by_date = {day["date"]: day for week in grid for day in week}
    assert by_date["2026-06-01"]["count"] == 2
    assert by_date["2026-06-01"]["failed"] is True
    assert by_date["2026-06-01"]["level"] == 2
    assert by_date["2026-06-03"]["level"] == 1
    assert by_date["2026-06-02"]["count"] == 0
    assert by_date["2026-06-12"]["future"] is True  # day after `today`
    # last column contains today
    assert any(day["date"] == "2026-06-11" for day in grid[-1])


def test_index_renders_health_gauge_and_pulse_strip(history_dir):
    resp = _client(history_dir).get("/")
    assert resp.status_code == 200
    assert "Fleet health" in resp.text
    assert "pulse-strip" in resp.text
    assert "palette-data" in resp.text  # command-palette blob on every page


def _palette_items(resp) -> list:
    """Parse the command-palette JSON blob base.html embeds on every page
    (``<script type="application/json" id="palette-data">``). Jinja's
    ``tojson`` escapes HTML-special characters, which ``json.loads`` decodes
    back, so labels and URLs with any content parse intact."""
    blob = resp.text.split('id="palette-data">', 1)[1].split("</script>", 1)[0]
    return json.loads(blob)


def test_palette_lxc_entries_use_canonical_node_id_urls(tmp_path):
    """PR3: LXC command-palette entries from the latest run AND the pending
    snapshot collapse onto one canonical /hosts/{node}/{id} entry labelled with
    the container name — the run's node-qualified error/warning records and the
    pending snapshot dedup onto that same URL, never leaving a bare
    /hosts/{id} or /hosts/{name} duplicate behind."""
    d = tmp_path / "history"
    state = _state(
        fleet_lxc_data=[dict(node="pve-01", name="sonarr", id="101", app="Updated: v4.0 → v4.1", os="OK")],
        fleet_error_log=[dict(host="pve-01/101", task="app update", error="boom")],
        fleet_warning_log=[dict(host="pve-01/101", task="snapshot", warning="snapshot slow")],
        fleet_changed=True,
        fleet_failed=True,
    )
    write_history(
        state, history_dir=d, keep=0, timestamp="20260102T000000000000Z", briefing=briefing.prepare_body(state)
    )
    scan_mod.write_pending(
        {
            "timestamp": "20260103T000000000000Z",
            "hosts": {"web-01": {"kind": "remote", "pkg_mgr": "apt", "pending_count": 0, "pending": [], "error": None}},
            # the same sonarr again, with the id field every scan emits
            "lxc": {
                "101": {
                    "node": "pve-01",
                    "id": "101",
                    "name": "sonarr",
                    "skipped": None,
                    "os_pending_count": 1,
                    "os_pending": ["libssl3"],
                    "app": None,
                    "error": None,
                }
            },
        },
        history_dir=d,
        keep=0,
    )

    hosts = [item for item in _palette_items(_client(d).get("/")) if item["kind"] == "host"]
    by_label = {item["label"]: item["url"] for item in hosts}

    # sonarr appears exactly once, displaying its container name, canonical URL
    assert sum(item["label"] == "sonarr" for item in hosts) == 1
    assert by_label["sonarr"] == "/hosts/pve-01/101"
    # remote host from the pending snapshot keeps its bare-name identity
    assert by_label["web-01"] == "/hosts/web-01"
    # no bare LXC id or bare container-name entry leaks in from any source
    urls = set(by_label.values())
    assert "/hosts/101" not in urls
    assert "/hosts/sonarr" not in urls


def test_palette_multi_cluster_lxc_ids_get_distinct_canonical_urls(tmp_path):
    """Two clusters' identically-numbered containers each get their own
    canonical /hosts/{node}/{id} palette entry — the bare /hosts/101 must not
    appear, and the run's pve-01/101 stays distinct from the pending ones."""
    d = tmp_path / "history"
    state = _state(
        fleet_lxc_data=[dict(node="pve-01", name="sonarr", id="101", app="Updated: v4.0 → v4.1", os="OK")],
        fleet_error_log=[dict(host="pve-01/101", task="app update", error="boom")],
        fleet_changed=True,
        fleet_failed=True,
    )
    write_history(
        state, history_dir=d, keep=0, timestamp="20260102T000000000000Z", briefing=briefing.prepare_body(state)
    )
    scan_mod.write_pending(
        {
            "timestamp": "20260104T000000000000Z",
            "hosts": {},
            "lxc": {
                "alpha-01/101": {
                    "node": "alpha-01",
                    "id": "101",
                    "name": "sonarr",
                    "skipped": None,
                    "os_pending_count": 1,
                    "os_pending": ["libssl3"],
                    "app": None,
                    "error": None,
                },
                "beta-01/101": {
                    "node": "beta-01",
                    "id": "101",
                    "name": "radarr",
                    "skipped": None,
                    "os_pending_count": 0,
                    "os_pending": [],
                    "app": None,
                    "error": None,
                },
            },
        },
        history_dir=d,
        keep=0,
    )

    hosts = [item for item in _palette_items(_client(d).get("/")) if item["kind"] == "host"]
    urls = {item["url"] for item in hosts}
    label_by_url = {item["url"]: item["label"] for item in hosts}

    # three containers share the id 101 — all three get canonical node/id URLs
    assert urls == {"/hosts/pve-01/101", "/hosts/alpha-01/101", "/hosts/beta-01/101"}
    assert label_by_url["/hosts/pve-01/101"] == "sonarr"
    assert label_by_url["/hosts/alpha-01/101"] == "sonarr"
    assert label_by_url["/hosts/beta-01/101"] == "radarr"
    assert "/hosts/101" not in urls


def test_history_renders_activity_heatmap(history_dir):
    resp = _client(history_dir).get("/history")
    assert resp.status_code == 200
    assert "Activity" in resp.text
    assert "heatmap" in resp.text


def test_pending_page_renders_health_signals(history_dir):
    """Low disk and an out-of-date container OS must be visible on the page."""
    scan_mod.write_pending(
        {
            "timestamp": "20260104T000000000000Z",
            "hosts": {},
            "lxc": {
                "130": {
                    "node": "Hammond",
                    "name": "grafana",
                    "skipped": None,
                    "os_pending_count": 4,
                    "os_pending": ["grafana"],
                    "app": None,
                    "disk_percent": 90,
                    "os": "debian 13",
                    "os_mismatch": None,
                    "error": None,
                },
                "123": {
                    "node": "Hammond",
                    "name": "nginxproxymanager",
                    "skipped": None,
                    "os_pending_count": 0,
                    "os_pending": [],
                    "app": None,
                    "disk_percent": 63,
                    "os": "debian 12",
                    "os_mismatch": "container runs debian 12 but ct/nginxproxymanager.sh targets debian 13",
                    "error": None,
                },
            },
        },
        history_dir=history_dir,
        keep=0,
    )

    resp = _client(history_dir).get("/pending", params={"ref": "20260104T000000000000Z"})
    assert resp.status_code == 200
    assert "90%" in resp.text  # over the 75% default → flagged
    assert "63%" in resp.text
    assert "debian 12 — outdated" in resp.text  # the mismatch pill
    assert "targets debian 13" in resp.text  # full reason in the tooltip


def test_pending_page_tolerates_scans_without_health_keys(history_dir):
    """The seeded fixture predates the health fields — must still render."""
    resp = _client(history_dir).get("/pending")
    assert resp.status_code == 200
    assert "sonarr" in resp.text


def test_pending_page_renders_health_signals_on_node_keyed_snapshots(history_dir):
    """The intersection of #38 and #41: node/id keys AND the health columns.

    Neither branch covered this on its own — #38's snapshots are keyed
    "node/id" while #41's Disk/OS columns render from the same entries.
    """
    scan_mod.write_pending(
        {
            "timestamp": "20260105T000000000000Z",
            "hosts": {},
            "lxc": {
                "Hammond/130": {
                    "node": "Hammond",
                    "id": "130",
                    "name": "grafana",
                    "skipped": None,
                    "os_pending_count": 4,
                    "os_pending": ["grafana"],
                    "app": None,
                    "disk_percent": 90,
                    "os": "debian 13",
                    "os_mismatch": None,
                    "error": None,
                },
                "Hammond/123": {
                    "node": "Hammond",
                    "id": "123",
                    "name": "nginxproxymanager",
                    "skipped": None,
                    "os_pending_count": 0,
                    "os_pending": [],
                    "app": None,
                    "disk_percent": 63,
                    "os": "debian 12",
                    "os_mismatch": "container runs debian 12 but ct/nginxproxymanager.sh targets debian 13",
                    "error": None,
                },
            },
        },
        history_dir=history_dir,
        keep=0,
    )

    resp = _client(history_dir).get("/pending", params={"ref": "20260105T000000000000Z"})
    assert resp.status_code == 200
    assert "90%" in resp.text and "63%" in resp.text
    assert "debian 12 — outdated" in resp.text
    # the display id column shows the bare id, not the raw "Hammond/130" key
    assert ">130<" in resp.text and ">123<" in resp.text
    # the composite key now appears only inside the drill-down link href (Task 5),
    # never as a visible id-column value
    assert 'href="/hosts/Hammond/130"' in resp.text


def test_pending_page_shows_unreachable_as_skipped_not_an_error(history_dir):
    """An unreachable host reads as "could not look", not a red failure."""
    scan_mod.write_pending(
        {
            "timestamp": "20260106T000000000000Z",
            "hosts": {
                "ONeill": {
                    "kind": "node",
                    "pkg_mgr": "",
                    "pending_count": 0,
                    "pending": [],
                    "unreachable": True,
                    "error": "No route to host",
                }
            },
            "lxc": {
                "ONeill": {
                    "node": "ONeill",
                    "id": "ONeill",
                    "name": "ONeill",
                    "skipped": "unreachable",
                    "os_pending_count": 0,
                    "os_pending": [],
                    "app": None,
                    "disk_percent": None,
                    "os": "",
                    "os_mismatch": None,
                    "unreachable": True,
                    "error": "discovery failed: node unreachable",
                }
            },
        },
        history_dir=history_dir,
        keep=0,
    )

    resp = _client(history_dir).get("/pending", params={"ref": "20260106T000000000000Z"})
    assert resp.status_code == 200
    assert "unreachable — skipped" in resp.text
    assert "skipped (unreachable)" in resp.text
    # the raw error is a tooltip, not a red cell
    assert '<span class="fail">No route to host</span>' not in resp.text


# --- PR2: security + reboot-required columns/stats ----------------------------- #


def test_pending_page_shows_security_and_reboot(history_dir):
    """PR2: the Security column renders a fail pill when >0 (names as tooltip)
    and the reboot-required flag renders as a warn pill."""
    scan_mod.write_pending(
        {
            "timestamp": "20260107T000000000000Z",
            "hosts": {
                "web-01": {
                    "kind": "remote",
                    "pkg_mgr": "apt",
                    "pending_count": 2,
                    "pending": ["curl", "openssl"],
                    "security_count": 1,
                    "security": ["curl"],
                    "reboot_required": True,
                    "error": None,
                }
            },
            "lxc": {
                "130": {
                    "node": "pve-01",
                    "id": "130",
                    "name": "grafana",
                    "skipped": None,
                    "os_pending_count": 4,
                    "os_pending": ["grafana"],
                    "app": None,
                    "os_security_count": 2,
                    "os_security": ["openssl", "curl"],
                    "reboot_required": True,
                    "disk_percent": None,
                    "os": "debian 13",
                    "os_mismatch": None,
                    "error": None,
                },
            },
        },
        history_dir=history_dir,
        keep=0,
    )

    resp = _client(history_dir).get("/pending", params={"ref": "20260107T000000000000Z"})
    assert resp.status_code == 200
    # security fail pills, with the package names in the tooltip
    assert '<span class="pill fail" title="curl">1</span>' in resp.text
    assert '<span class="pill fail" title="openssl, curl">2</span>' in resp.text
    # reboot pills on both the host row and the container row
    assert resp.text.count("reboot required") == 2


def test_pending_page_legacy_snapshot_without_security_keys(history_dir):
    """Pre-PR2 snapshots lack security/reboot keys — the cells render zeros
    and dashes, never a template error."""
    scan_mod.write_pending(
        {
            "timestamp": "20260106T000000000000Z",
            "hosts": {
                "web-01": {"kind": "remote", "pkg_mgr": "apt", "pending_count": 1, "pending": ["curl"], "error": None}
            },
            "lxc": {
                "130": {
                    "node": "pve-01",
                    "id": "130",
                    "name": "grafana",
                    "skipped": None,
                    "os_pending_count": 2,
                    "os_pending": ["a"],
                    "app": None,
                    "disk_percent": None,
                    "os": "debian 13",
                    "os_mismatch": None,
                    "error": None,
                },
            },
        },
        history_dir=history_dir,
        keep=0,
    )

    resp = _client(history_dir).get("/pending", params={"ref": "20260106T000000000000Z"})
    assert resp.status_code == 200
    assert "curl" in resp.text and "grafana" in resp.text
    # no security pill, no reboot pill, no failures
    assert "pill fail" not in resp.text
    assert "reboot required" not in resp.text
    assert "ZeroDivisionError" not in resp.text


def test_pending_page_past_scans_show_security_and_reboot_aggregates(history_dir):
    """The past-scans table surfaces security_pending and reboot_hosts."""
    scan_mod.write_pending(
        {
            "timestamp": "20260107T000000000000Z",
            "hosts": {
                "web-01": {
                    "kind": "remote",
                    "pkg_mgr": "apt",
                    "pending_count": 2,
                    "pending": ["curl"],
                    "security_count": 1,
                    "security": ["curl"],
                    "reboot_required": True,
                    "error": None,
                }
            },
            "lxc": {
                "130": {
                    "node": "pve-01",
                    "id": "130",
                    "name": "grafana",
                    "skipped": None,
                    "os_pending_count": 4,
                    "os_pending": ["grafana"],
                    "app": None,
                    "os_security_count": 2,
                    "os_security": ["a", "b"],
                    "reboot_required": True,
                    "disk_percent": None,
                    "os": "debian 13",
                    "os_mismatch": None,
                    "error": None,
                }
            },
        },
        history_dir=history_dir,
        keep=0,
    )

    resp = _client(history_dir).get("/pending", params={"ref": "20260107T000000000000Z"})
    assert resp.status_code == 200
    # aggregate row: 1 + 2 security pkgs, 2 reboot-required entries
    assert "Security pkgs" in resp.text and "Reboot" in resp.text
    assert '<span class="fail">3</span>' in resp.text
    assert '<span class="fail">2</span>' in resp.text


def test_overview_pending_card_shows_security_and_reboot_stats(history_dir):
    """PR2: the overview's pending card surfaces security pkgs + reboot count."""
    scan_mod.write_pending(
        {
            "timestamp": "20260108T000000000000Z",
            "hosts": {
                "web-01": {
                    "kind": "remote",
                    "pkg_mgr": "apt",
                    "pending_count": 2,
                    "security_count": 1,
                    "security": ["curl"],
                    "reboot_required": True,
                    "error": None,
                }
            },
            "lxc": {
                "130": {
                    "node": "pve-01",
                    "id": "130",
                    "name": "grafana",
                    "skipped": None,
                    "os_pending_count": 4,
                    "os_pending": ["grafana"],
                    "app": None,
                    "os_security_count": 2,
                    "os_security": ["a", "b"],
                    "reboot_required": True,
                    "disk_percent": None,
                    "os": "debian 13",
                    "os_mismatch": None,
                    "error": None,
                }
            },
        },
        history_dir=history_dir,
        keep=0,
    )

    resp = _client(history_dir).get("/")
    assert resp.status_code == 200
    assert "security pkgs" in resp.text
    assert "reboot required" in resp.text
    # 1 host + 2 containers' security packages, and 2 reboot-required entries
    assert '<span class="value">3</span>' in resp.text
    assert resp.text.count('<span class="value">2</span>') >= 1


def test_health_score_deducts_security_and_reboot():
    row = {"security_pending": 3, "reboot_hosts": 2, "outdated_apps": 1}
    assert _health_score(None, row) == 100 - 1 - 2 * 3 - 2 * 2


def test_health_score_security_and_reboot_caps():
    """The deductions are capped (-20 security, -10 reboot) like the others."""
    row = {"security_pending": 100, "reboot_hosts": 100, "outdated_apps": 0}
    assert _health_score(None, row) == 100 - 20 - 10


def test_health_score_legacy_pending_row_no_deduction():
    """Pre-PR2 rows lack the keys — .get() defaults keep the score unchanged."""
    assert _health_score(None, {"outdated_apps": 0}) == 100
    assert _health_score(None, None) == 100
    run = {"failed": True, "counts": {"errors": 1}}
    assert _health_score(run, {"outdated_apps": 0}) == 100 - 25 - 5


# --- PR3: per-host ledger metadata, OS-upgrade timeline, overview ---------- #


def _seed_ledger_upgrade(history_dir):
    """A run that applied an OS update (ledger last_changed_ts on the LXC) plus
    a scan baseline → upgrade for a remote host (an os-upgrade event), and a
    scan-only host (db-01) that upgrades without ever appearing in a run."""
    state = _state(
        fleet_lxc_data=[dict(node="pve-01", name="sonarr", id="101", app="OK", os="Updated (2 upgraded)", snap=True)],
        fleet_remote_data=[dict(host="web-01", status="UPDATED")],
        fleet_changed=True,
    )
    write_history(
        state,
        history_dir=history_dir,
        keep=0,
        timestamp="20260101T000000000000Z",
        briefing=briefing.prepare_body(state),
    )
    scan_mod.write_pending(
        {
            "timestamp": "20260102T000000000000Z",
            "hosts": {
                "web-01": {
                    "kind": "remote",
                    "pkg_mgr": "apt",
                    "pending_count": 0,
                    "pending": [],
                    "error": None,
                    "os_release": {"id": "debian", "version_id": "11", "pretty_name": "Debian GNU/Linux 11 (bullseye)"},
                },
                "db-01": {
                    "kind": "remote",
                    "pkg_mgr": "apt",
                    "pending_count": 0,
                    "pending": [],
                    "error": None,
                    "os_release": {"id": "debian", "version_id": "10", "pretty_name": "Debian GNU/Linux 10 (buster)"},
                },
            },
            "lxc": {
                "pve-01/101": {
                    "node": "pve-01",
                    "id": "101",
                    "name": "sonarr",
                    "skipped": None,
                    "os_pending_count": 0,
                    "os_pending": [],
                    "error": None,
                    "os_release": {"id": "debian", "version_id": "12", "pretty_name": "Debian GNU/Linux 12 (bookworm)"},
                }
            },
        },
        history_dir=history_dir,
        keep=0,
    )
    scan_mod.write_pending(
        {
            "timestamp": "20260103T000000000000Z",
            "hosts": {
                "web-01": {
                    "kind": "remote",
                    "pkg_mgr": "apt",
                    "pending_count": 0,
                    "pending": [],
                    "error": None,
                    "os_release": {"id": "debian", "version_id": "12", "pretty_name": "Debian GNU/Linux 12 (bookworm)"},
                },
                "db-01": {
                    "kind": "remote",
                    "pkg_mgr": "apt",
                    "pending_count": 0,
                    "pending": [],
                    "error": None,
                    "os_release": {"id": "debian", "version_id": "11", "pretty_name": "Debian GNU/Linux 11 (bullseye)"},
                },
            },
            "lxc": {
                "pve-01/101": {
                    "node": "pve-01",
                    "id": "101",
                    "name": "sonarr",
                    "skipped": None,
                    "os_pending_count": 0,
                    "os_pending": [],
                    "error": None,
                    "os_release": {"id": "debian", "version_id": "12", "pretty_name": "Debian GNU/Linux 12 (bookworm)"},
                }
            },
        },
        history_dir=history_dir,
        keep=0,
    )


def test_host_page_shows_ledger_metadata(history_dir):
    """PR3: the host header shows 'last updated', 'last run' and the OS label
    (Debian 12 …) from the ledger — relative data-ts spans for the dates."""
    _seed_ledger_upgrade(history_dir)
    resp = _client(history_dir).get("/hosts/pve-01/101")
    assert resp.status_code == 200
    assert "last updated" in resp.text
    assert "last run" in resp.text
    assert "Debian GNU/Linux 12 (bookworm)" in resp.text
    # relative-time spans (client-side ticking) with UTC fallback text
    assert 'data-ts="2026-01-01T00:00:00Z"' in resp.text
    assert "2026-01-01 00:00 UTC" in resp.text


def test_host_page_composite_key_resolves_ledger(history_dir):
    """The composite node/id URL (multi-cluster-safe LXC identity) resolves the
    same ledger entry the run records use — the whole point of the adjustment."""
    _seed_ledger_upgrade(history_dir)
    resp = _client(history_dir).get("/hosts/pve-01/101")
    assert resp.status_code == 200
    assert "last updated" in resp.text
    # the same-id container on another node is a distinct host with no metadata
    other = _client(history_dir).get("/hosts/pve-02/101")
    assert other.status_code == 200
    assert "last updated" not in other.text
    assert "No records for this host" in other.text


def test_host_page_merged_os_upgrade_timeline(history_dir):
    """PR3: os-upgrade events are timeline items merged with run records and
    sorted newest-first alongside them."""
    _seed_ledger_upgrade(history_dir)
    resp = _client(history_dir).get("/hosts/web-01")
    assert resp.status_code == 200
    assert "os-upgrade" in resp.text
    assert '<span class="pill info">os-upgrade</span>' in resp.text
    assert "11" in resp.text and "12" in resp.text
    # the event (scan ts 20260103) sorts above the run record (run ts 20260101)
    assert resp.text.index("os-upgrade") < resp.text.index("/history/20260101T000000000000Z")


def test_host_page_ledger_only_host_still_renders(history_dir):
    """A host seen only by scans (no run records) still gets metadata + event
    timeline items; the empty-state only applies when there is nothing at all."""
    _seed_ledger_upgrade(history_dir)
    resp = _client(history_dir).get("/hosts/db-01")
    assert resp.status_code == 200
    assert "last run" not in resp.text  # no run ever observed db-01
    assert "os-upgrade" in resp.text  # but the scan event is there
    assert "Debian GNU/Linux 11 (bullseye)" in resp.text  # current release label


def test_host_page_unknown_host_no_ledger_no_crash(history_dir):
    resp = _client(history_dir).get("/hosts/ghost")
    assert resp.status_code == 200
    assert "No records for this host" in resp.text


def test_overview_recent_os_upgrades(history_dir):
    """PR3: the overview shows a Recent OS upgrades list when the ledger has
    events — host link, from → to, and the scan timestamp."""
    _seed_ledger_upgrade(history_dir)
    resp = _client(history_dir).get("/")
    assert resp.status_code == 200
    assert "Recent OS upgrades" in resp.text
    assert 'href="/hosts/web-01"' in resp.text
    assert "bullseye" not in resp.text  # old release, not current
    assert 'href="/hosts/web-01"' in resp.text and "11" in resp.text and "12" in resp.text


def test_overview_no_os_upgrades_when_no_events(history_dir):
    """A fleet with no release changes shows no Recent OS upgrades section."""
    resp = _client(history_dir).get("/")
    assert resp.status_code == 200
    assert "Recent OS upgrades" not in resp.text


def test_run_detail_lxc_composite_link(history_dir):
    """PR3: the run-detail LXC name cell links to the composite /hosts/{node}/{id}
    URL (multi-cluster-safe), not the bare container name."""
    resp = _client(history_dir).get("/history/20260101T000000000000Z")
    assert resp.status_code == 200
    assert 'href="/hosts/pve-01/101"' in resp.text
    # no bare-name link for that container
    assert 'href="/hosts/sonarr"' not in resp.text


# --- PR4: host pending timeline + package search ----------------------------- #


def _seed_pr4(history_dir):
    """PR4 fixture: two runs with package detail, two retained pending
    snapshots (legacy bare-id lxc key then node/id key) with counts, and a
    deliberately divergent ``pending-latest.json`` alias the readers must
    never pick up (it would duplicate the newest timestamped snapshot)."""
    older = _state(
        fleet_vm_data=[
            dict(
                node="pve-01",
                vmid="200",
                name="my-vm",
                status="UPDATED",
                pkg_count=2,
                packages=[
                    {"name": "curl", "from": "7.1", "to": "7.2"},
                    {"name": "openssl", "from": "1.1", "to": "1.1.2"},
                ],
            )
        ],
        fleet_changed=True,
    )
    write_history(older, history_dir=history_dir, keep=0, timestamp="20260110T000000000000Z")
    newer = _state(
        fleet_lxc_data=[
            dict(
                node="pve-01",
                name="sonarr",
                id="101",
                os="UPDATED",
                app="OK",
                packages=[
                    {"name": "libssl3", "from": "3.0.11", "to": "3.0.13"},
                    {"name": "curl", "from": "", "to": "8.5.0"},
                ],
            )
        ],
        fleet_error_log=[dict(host="pve-01/101", task="app update", error="boom")],
        fleet_changed=True,
    )
    write_history(newer, history_dir=history_dir, keep=0, timestamp="20260111T000000000000Z")
    scan_mod.write_pending(
        {
            "timestamp": "20260112T000000000000Z",
            "hosts": {
                "web-01": {
                    "kind": "remote",
                    "pkg_mgr": "apt",
                    "pending_count": 2,
                    "pending": ["curl", "openssl"],
                    "security_count": 1,
                    "security": ["openssl"],
                    "error": None,
                }
            },
            "lxc": {
                "101": {
                    "node": "pve-01",
                    "name": "sonarr",
                    "skipped": None,
                    "os_pending_count": 1,
                    "os_pending": ["libssl3"],
                    "os_security_count": 1,
                    "os_security": ["libssl3"],
                    "app": {"script": "sonarr", "current": "4.0.17", "latest": "4.0.18", "outdated": True},
                    "reboot_required": True,
                    "error": None,
                }
            },
        },
        history_dir=history_dir,
        keep=0,
    )
    scan_mod.write_pending(
        {
            "timestamp": "20260113T000000000000Z",
            "hosts": {},
            "lxc": {
                "pve-01/101": {
                    "node": "pve-01",
                    "id": "101",
                    "name": "sonarr",
                    "skipped": None,
                    "os_pending_count": 0,
                    "os_pending": [],
                    "app": None,
                    "error": None,
                }
            },
        },
        history_dir=history_dir,
        keep=0,
    )
    # The alias file is written directly with a different payload than any
    # timestamped snapshot: an entry only it knows about.
    (Path(str(history_dir)) / "pending-latest.json").write_text(
        json.dumps(
            {
                "timestamp": "20990101T000000000000Z",
                "hosts": {
                    "only-in-latest": {
                        "kind": "remote",
                        "pkg_mgr": "apt",
                        "pending_count": 9,
                        "pending": [],
                        "error": None,
                    }
                },
                "lxc": {},
            }
        ),
        encoding="utf-8",
    )


# --- _host_pending_entries --------------------------------------------------- #


def test_host_pending_entries_newest_first_and_latest_alias_excluded(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    entries = _host_pending_entries(str(d), "sonarr")
    # newest timestamped snapshot first; the latest alias is never read
    assert [e["timestamp"] for e in entries] == ["20260113T000000000000Z", "20260112T000000000000Z"]
    assert all(e["kind"] == "lxc" for e in entries)
    assert _host_pending_entries(str(d), "only-in-latest") == []


def test_host_pending_entries_host_section_matches_by_name(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    entries = _host_pending_entries(str(d), "web-01")
    assert [e["timestamp"] for e in entries] == ["20260112T000000000000Z"]
    assert entries[0]["kind"] == "host"
    assert entries[0]["entry"]["pending_count"] == 2
    assert entries[0]["bucket"] == "pending"


def test_host_pending_entries_composite_and_bare_shapes(tmp_path):
    """The composite node/id name, the bare container name and the bare id
    all resolve the same entries — including the legacy bare-id-keyed
    snapshot (which predates the explicit ``id`` field)."""
    d = tmp_path / "history"
    _seed_pr4(d)
    assert len(_host_pending_entries(str(d), "pve-01/101")) == 2
    assert len(_host_pending_entries(str(d), "sonarr")) == 2
    assert len(_host_pending_entries(str(d), "101")) == 2


def test_host_pending_entries_multi_cluster_isolation(tmp_path):
    """A same-id container on another node never leaks into this host's
    entries under the composite name; the bare name is shared (documented
    behaviour, mirroring run-record matching)."""
    d = tmp_path / "history"
    scan_mod.write_pending(
        {
            "timestamp": "20260112T000000000000Z",
            "hosts": {},
            "lxc": {
                "alpha-01/101": {
                    "node": "alpha-01",
                    "id": "101",
                    "name": "sonarr",
                    "skipped": None,
                    "os_pending_count": 0,
                    "os_pending": [],
                    "error": None,
                }
            },
        },
        history_dir=d,
        keep=0,
    )
    assert len(_host_pending_entries(str(d), "pve-01/101")) == 0
    assert len(_host_pending_entries(str(d), "alpha-01/101")) == 1
    assert len(_host_pending_entries(str(d), "sonarr")) == 1


def test_host_pending_entries_corrupt_snapshot_skipped(tmp_path):
    d = tmp_path / "history"
    scan_mod.write_pending({"timestamp": "20260112T000000000000Z", "hosts": {}, "lxc": {}}, history_dir=d, keep=0)
    (d / "pending-20260113T000000000000Z.json").write_text("{corrupt", encoding="utf-8")
    scan_mod.write_pending(
        {
            "timestamp": "20260114T000000000000Z",
            "hosts": {},
            "lxc": {
                "101": {
                    "node": "pve-01",
                    "name": "sonarr",
                    "skipped": None,
                    "os_pending_count": 1,
                    "os_pending": ["x"],
                    "error": None,
                }
            },
        },
        history_dir=d,
        keep=0,
    )
    entries = _host_pending_entries(str(d), "sonarr")
    assert [e["timestamp"] for e in entries] == ["20260114T000000000000Z"]


def test_host_pending_entries_missing_timestamp_uses_filename(tmp_path):
    """A pending snapshot that omits its `timestamp` field still lands on the
    timeline at the timestamp encoded in its filename (pending-<ts>.json)."""
    d = tmp_path / "history"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pending-20260112T000000000000Z.json").write_text(
        json.dumps(
            {
                "hosts": {
                    "web-01": {
                        "kind": "remote",
                        "pkg_mgr": "apt",
                        "pending_count": 2,
                        "pending": ["curl"],
                        "error": None,
                    }
                },
                "lxc": {
                    "101": {
                        "node": "pve-01",
                        "name": "sonarr",
                        "skipped": None,
                        "os_pending_count": 1,
                        "os_pending": ["libssl3"],
                        "error": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    entries = _host_pending_entries(str(d), "sonarr")
    assert len(entries) == 1
    assert entries[0]["timestamp"] == "20260112T000000000000Z"
    assert entries[0]["bucket"] == "pending" and entries[0]["kind"] == "lxc"
    # the merged timeline sorts the filename fallback against run records
    write_history(
        _state(
            fleet_lxc_data=[dict(node="pve-01", name="sonarr", id="101", os="UPDATED", app="OK")], fleet_changed=True
        ),
        history_dir=d,
        keep=0,
        timestamp="20260111T000000000000Z",
    )
    timeline = _host_timeline(str(d), "sonarr", [])
    assert [e["timestamp"] for e in timeline] == ["20260112T000000000000Z", "20260111T000000000000Z"]


def test_host_pending_entries_unknown_host_empty(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    assert _host_pending_entries(str(d), "ghost") == []


# --- host page pending timeline ---------------------------------------------- #


def test_host_page_pending_timeline_lxc_entry(tmp_path):
    """The timeline renders a pending-snapshot lxc entry: OS/security counts,
    reboot pill, app current→latest, and the OS-package disclosure."""
    d = tmp_path / "history"
    _seed_pr4(d)
    resp = _client(d).get("/hosts/pve-01/101")
    assert resp.status_code == 200
    text = resp.text
    assert ">pending</span>" in text
    assert "OS</strong> 1 pending" in text
    assert "1 security" in text
    assert "reboot required" in text
    assert "4.0.17 → 4.0.18" in text
    assert "outdated" in text
    assert "libssl3" in text
    assert ">1 pkg(s)</summary>" in text
    assert 'href="/pending?ref=20260113T000000000000Z"' in text


def test_host_page_pending_timeline_host_entry(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    resp = _client(d).get("/hosts/web-01")
    assert resp.status_code == 200
    text = resp.text
    assert ">pending</span>" in text
    assert "OS</strong> 2 pending" in text
    assert "1 security" in text
    assert ">2 pkg(s)</summary>" in text
    assert "curl" in text and "openssl" in text


def test_host_page_timeline_newest_first_interleaved(tmp_path):
    """Pending entries, run records and ledger events interleave in one
    newest-first timeline (lexical timestamp sort)."""
    d = tmp_path / "history"
    _seed_pr4(d)
    text = _client(d).get("/hosts/sonarr").text
    assert (
        text.index("/pending?ref=20260113T000000000000Z")
        < text.index("/pending?ref=20260112T000000000000Z")
        < text.index("/history/20260111T000000000000Z")
    )


def test_host_page_timeline_merges_pending_events_and_runs(history_dir):
    """PR3 events + PR4 pending entries + run records share one timeline;
    the newest pending entry sorts above the os-upgrade event."""
    _seed_ledger_upgrade(history_dir)
    scan_mod.write_pending(
        {
            "timestamp": "20260104T000000000000Z",
            "hosts": {
                "web-01": {"kind": "remote", "pkg_mgr": "apt", "pending_count": 2, "pending": ["curl"], "error": None}
            },
            "lxc": {},
        },
        history_dir=history_dir,
        keep=0,
    )
    text = _client(history_dir).get("/hosts/web-01").text
    assert "os-upgrade" in text
    assert ">pending</span>" in text
    assert "OS</strong> 2 pending" in text
    assert (
        text.index("/pending?ref=20260104") < text.index("os-upgrade") < text.index("/history/20260101T000000000000Z")
    )


def test_host_page_pending_timeline_defensive_for_legacy(tmp_path):
    """Snapshots predating PR2's security/reboot keys and with bare-id lxc
    keys still render — counts read 0, no security/reboot pills, no crash."""
    d = tmp_path / "history"
    scan_mod.write_pending(
        {
            "timestamp": "20260112T000000000000Z",
            "hosts": {
                "web-01": {"kind": "remote", "pkg_mgr": "apt", "pending_count": 3, "pending": ["curl"], "error": None}
            },
            "lxc": {
                "101": {
                    "node": "pve-01",
                    "name": "sonarr",
                    "skipped": None,
                    "os_pending_count": 2,
                    "os_pending": ["libssl3", "openssl"],
                    "error": None,
                }
            },
        },
        history_dir=d,
        keep=0,
    )
    for path in ("/hosts/sonarr", "/hosts/101", "/hosts/pve-01/101", "/hosts/web-01"):
        assert _client(d).get(path).status_code == 200
    text = _client(d).get("/hosts/pve-01/101").text
    assert ">pending</span>" in text
    assert "OS</strong> 2 pending" in text
    assert "libssl3" in text and "openssl" in text
    assert "security</span>" not in text
    assert "reboot required" not in text


def test_host_page_pending_timeline_errors_and_skips(tmp_path):
    d = tmp_path / "history"
    scan_mod.write_pending(
        {
            "timestamp": "20260112T000000000000Z",
            "hosts": {},
            "lxc": {
                "pve-01/101": {
                    "node": "pve-01",
                    "id": "101",
                    "name": "sonarr",
                    "skipped": "stopped",
                    "os_pending_count": 0,
                    "os_pending": [],
                    "error": None,
                }
            },
        },
        history_dir=d,
        keep=0,
    )
    text = _client(d).get("/hosts/pve-01/101").text
    assert "skipped (stopped)" in text
    scan_mod.write_pending(
        {
            "timestamp": "20260113T000000000000Z",
            "hosts": {},
            "lxc": {
                "pve-01/101": {
                    "node": "pve-01",
                    "id": "101",
                    "name": "sonarr",
                    "skipped": "unreachable",
                    "os_pending_count": 0,
                    "os_pending": [],
                    "unreachable": True,
                    "error": "discovery failed: node unreachable",
                }
            },
        },
        history_dir=d,
        keep=0,
    )
    text = _client(d).get("/hosts/pve-01/101").text
    assert "unreachable — skipped" in text
    assert "discovery failed" in text


def test_host_timeline_merges_records_pending_and_events(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    events = [{"type": "os-upgrade", "host": "sonarr", "from": "12", "to": "13", "ts": "20260114T000000000000Z"}]
    timeline = _host_timeline(str(d), "sonarr", events)
    assert [(e.get("bucket") or e.get("kind")) for e in timeline] == ["event", "pending", "pending", "lxc"]


# --- _search_packages -------------------------------------------------------- #


def test_search_packages_blank_and_stripped(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    assert _search_packages(str(d), "") == []
    assert _search_packages(str(d), "   ") == []
    assert len(_search_packages(str(d), "  curl  ")) == 2


def test_search_packages_case_insensitive(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    assert len(_search_packages(str(d), "CURL")) == 2
    assert len(_search_packages(str(d), "OpenSSL")) == 1
    assert len(_search_packages(str(d), "LIBSSL3")) == 1


def test_search_packages_matches_from_and_to_versions(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    hits = _search_packages(str(d), "7.1")
    assert len(hits) == 1 and hits[0]["name"] == "curl" and hits[0]["from"] == "7.1"
    hits = _search_packages(str(d), "3.0.13")
    assert len(hits) == 1 and hits[0]["name"] == "libssl3" and hits[0]["to"] == "3.0.13"
    hits = _search_packages(str(d), "1.1.2")
    assert len(hits) == 1 and hits[0]["name"] == "openssl"


def test_search_packages_ordering_and_normalization(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    hits = _search_packages(str(d), "curl")
    assert [h["timestamp"] for h in hits] == ["20260111T000000000000Z", "20260110T000000000000Z"]
    newer, older = hits
    # lxc hit: canonical node/id identity + host URL, empty from (new-install shape)
    assert newer["bucket"] == "lxc"
    assert newer["host"] == "sonarr"
    assert newer["identity"] == "pve-01/101"
    assert newer["host_url"] == "/hosts/pve-01/101"
    assert newer["run_url"] == "/history/20260111T000000000000Z"
    assert newer["name"] == "curl" and newer["from"] == "" and newer["to"] == "8.5.0"
    # vm hit: name identity, both versions
    assert older["bucket"] == "vm"
    assert older["host"] == "my-vm"
    assert older["identity"] == "my-vm"
    assert older["host_url"] == "/hosts/my-vm"
    assert older["run_url"] == "/history/20260110T000000000000Z"
    assert older["name"] == "curl" and older["from"] == "7.1" and older["to"] == "7.2"


def test_search_packages_filters_nonmatching(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    hits = _search_packages(str(d), "curl")
    assert {h["name"] for h in hits} == {"curl"}
    assert {h["bucket"] for h in hits} == {"lxc", "vm"}


def test_search_packages_miss(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    assert _search_packages(str(d), "zzz-nope") == []


def test_search_packages_no_duplicates(tmp_path):
    """Two identical package entries in one run collapse to one hit."""
    d = tmp_path / "history"
    state = _state(
        fleet_remote_data=[
            dict(host="web-01", status="UPDATED", packages=[{"name": "curl", "from": "1", "to": "2"}]),
            dict(host="web-01", status="UPDATED", packages=[{"name": "curl", "from": "1", "to": "2"}]),
        ],
        fleet_changed=True,
    )
    write_history(state, history_dir=d, keep=0, timestamp="20260101T000000000000Z")
    assert len(_search_packages(str(d), "curl")) == 1


def test_search_packages_same_name_lxc_on_different_nodes_not_deduped(tmp_path):
    """Two LXC records with the same container name and the same package on
    different nodes are distinct hosts: both survive dedup and each hit
    carries its own node/id identity and host URL."""
    d = tmp_path / "history"
    state = _state(
        fleet_lxc_data=[
            dict(
                node="pve-01",
                name="sonarr",
                id="101",
                os="UPDATED",
                app="OK",
                packages=[{"name": "curl", "from": "1", "to": "2"}],
            ),
            dict(
                node="pve-02",
                name="sonarr",
                id="101",
                os="UPDATED",
                app="OK",
                packages=[{"name": "curl", "from": "1", "to": "2"}],
            ),
        ],
        fleet_changed=True,
    )
    write_history(state, history_dir=d, keep=0, timestamp="20260101T000000000000Z")
    hits = _search_packages(str(d), "curl")
    assert len(hits) == 2
    assert {h["identity"] for h in hits} == {"pve-01/101", "pve-02/101"}
    assert {h["host_url"] for h in hits} == {"/hosts/pve-01/101", "/hosts/pve-02/101"}
    for hit in hits:
        assert hit["bucket"] == "lxc"
        assert hit["host"] == "sonarr"  # shared bare name, not a dedup key
        assert hit["name"] == "curl" and hit["from"] == "1" and hit["to"] == "2"
    """Records missing the `from` key (dnf real-run shape) and node/id
    identity still match — normalized with empty from and a name link."""
    d = tmp_path / "history"
    state = _state(
        fleet_remote_data=[
            dict(host="web-01", status="UPDATED", packages=[{"name": "curl", "to": "7.3"}]),
        ],
        fleet_changed=True,
    )
    write_history(state, history_dir=d, keep=0, timestamp="20260101T000000000000Z")
    hits = _search_packages(str(d), "curl")
    assert len(hits) == 1
    assert hits[0]["from"] == "" and hits[0]["to"] == "7.3"
    assert hits[0]["host"] == "web-01" and hits[0]["host_url"] == "/hosts/web-01"


def test_search_packages_never_reads_latest_alias(tmp_path):
    """latest.json is never searched: a package only it carries yields no
    hit, and its presence doesn't duplicate the newest run's hits."""
    d = tmp_path / "history"
    _seed_pr4(d)
    (d / "latest.json").write_text(
        json.dumps(
            {
                "timestamp": "20990101T000000000000Z",
                "changed": True,
                "failed": False,
                "counts": {},
                "vm": [
                    {
                        "node": "pve-01",
                        "vmid": "999",
                        "name": "latest-only",
                        "status": "UPDATED",
                        "packages": [{"name": "zzz-latest-only", "from": "", "to": "1.0"}],
                    }
                ],
                "lxc": [],
                "remote": [],
                "node": [],
                "custom": [],
                "errors": [],
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    assert _search_packages(str(d), "zzz-latest-only") == []
    assert len(_search_packages(str(d), "curl")) == 2


def test_search_packages_corrupt_run_skipped(tmp_path):
    d = tmp_path / "history"
    _seed_pr4(d)
    (d / "run-20260112T000000000000Z.json").write_text("{corrupt", encoding="utf-8")
    assert len(_search_packages(str(d), "curl")) == 2


# --- /packages route --------------------------------------------------------- #


def test_packages_requires_login(history_dir):
    assert _client(history_dir, login=False).get("/packages").status_code == 401
    assert _client(history_dir, login=False).get("/packages", params={"q": "curl"}).status_code == 401


def test_packages_blank_state(history_dir):
    resp = _client(history_dir).get("/packages")
    assert resp.status_code == 200
    assert "Package search" in resp.text
    assert 'name="q"' in resp.text
    assert "Type a package name or version" in resp.text
    assert "fleet_package_detail_keep" in resp.text
    # no results section yet
    assert "hit(s) for" not in resp.text


def test_packages_miss_state(history_dir):
    resp = _client(history_dir).get("/packages", params={"q": "zzz-nope"})
    assert resp.status_code == 200
    assert "No package matches" in resp.text
    assert "zzz-nope" in resp.text


def test_packages_grouped_results_hits_and_links(history_dir):
    """Search hits are grouped by run (newest first) with run + canonical
    host links, and each row carries a copy button with the exact package
    name/from/to."""
    _seed_pr4(history_dir)
    resp = _client(history_dir).get("/packages", params={"q": "curl"})
    assert resp.status_code == 200
    text = resp.text
    assert "2 hit(s) for" in text
    assert 'href="/history/20260111T000000000000Z"' in text
    assert 'href="/history/20260110T000000000000Z"' in text
    # canonical lxc link + name link
    assert 'href="/hosts/pve-01/101"' in text
    assert 'href="/hosts/my-vm"' in text
    # newer run's group renders first
    assert text.index("20260111T000000000000Z") < text.index("20260110T000000000000Z")
    # copy buttons carry name + from + to
    assert 'class="copy-pkg' in text
    assert 'data-copy="curl  8.5.0"' in text
    assert 'data-copy="curl 7.1 7.2"' in text


def test_packages_echoes_query(history_dir):
    resp = _client(history_dir).get("/packages", params={"q": "  openssl "})
    compact = " ".join(resp.text.split())
    assert 'name="q" value="openssl"' in compact


def test_packages_retention_note_uses_setting(history_dir):
    resp = _client(history_dir, fleet_package_detail_keep=3).get("/packages")
    assert "package detail is kept on the newest 3 run(s)" in resp.text
    assert "fleet_package_detail_keep" in resp.text


def test_packages_retention_note_keep_disabled(history_dir):
    resp = _client(history_dir, fleet_package_detail_keep=0).get("/packages")
    assert "retained on every run" in resp.text


def test_packages_result_state_legacy_and_version_queries(history_dir):
    """Version-only queries and legacy (no-from) records render end to end."""
    _seed_pr4(history_dir)
    resp = _client(history_dir).get("/packages", params={"q": "3.0.13"})
    assert resp.status_code == 200
    assert "1 hit(s) for" in resp.text
    assert "libssl3" in resp.text
    assert 'href="/hosts/pve-01/101"' in resp.text


# --- nav + command palette --------------------------------------------------- #


def test_nav_includes_packages_link(history_dir):
    resp = _client(history_dir).get("/")
    assert 'href="/packages"' in resp.text
    assert ">Packages<" in resp.text


def test_nav_packages_active_on_packages_page(history_dir):
    resp = _client(history_dir).get("/packages")
    assert 'href="/packages" class="active"' in resp.text


def test_palette_includes_package_search_page(history_dir):
    items = _palette_items(_client(history_dir).get("/"))
    pages = {i["label"]: i["url"] for i in items if i["kind"] == "page"}
    assert pages["Search packages"] == "/packages"
