"""Tests for proxmox_fleet.scan — the read-only pending-updates walk.

Parsers are tested directly; scan_host/scan_lxc/run_fleet_scan run against
scripted executors (no Ansible/PVE), with GitHub HTTP monkeypatched.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from proxmox_fleet import http as http_mod
from proxmox_fleet import scan as scan_mod
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult


def _ok(stdout="", rc=0):
    return PrimitiveResult(rc=rc, changed=False, stdout=stdout, failed=False)


def _fail(rc=1, stderr="boom"):
    return PrimitiveResult(rc=rc, failed=True, stderr=stderr)


class ScriptedExecutor:
    host = "web-01"

    def __init__(self, script=None, default=None, introspect_facts=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default if default is not None else _ok()
        self.commands: List[str] = []
        self.introspect_facts: Dict[str, Any] = introspect_facts or {}

    def _resp(self, command):
        for key, queue in self.script.items():
            if key in command and queue:
                return queue.pop(0)
        return self.default

    def run_shell(self, command, **opts):
        self.commands.append(command)
        return self._resp(command)

    def introspect(self, lxc_id):
        return PrimitiveResult(rc=0, changed=False, facts=dict(self.introspect_facts))


APT_SIM = """\
NOTE: This is only a simulation!
Inst libssl3 [3.0.11-1] (3.0.13-1 Debian:12.5/stable [amd64])
Conf libssl3 (3.0.13-1 Debian:12.5/stable [amd64])
Inst curl [7.88.1-10] (7.88.1-11 Debian-Security:12/stable-security [amd64])
Conf curl (7.88.1-11 Debian-Security:12/stable-security [amd64])
"""

DNF_CHECK = """\
kernel.x86_64        5.14.0-432.el9        baseos
openssl.x86_64       1:3.0.7-27.el9        appstream
"""

APK_LIST = """\
Installed:               Available:
musl-1.2.4-r1            < 1.2.4-r2
busybox-1.36.1-r4        < 1.36.1-r5
"""


# --- parse_pending -------------------------------------------------------------

def test_parse_pending_apt():
    assert scan_mod.parse_pending(APT_SIM, "apt") == ["libssl3", "curl"]


def test_parse_pending_dnf():
    assert scan_mod.parse_pending(DNF_CHECK, "dnf") == ["kernel", "openssl"]


def test_parse_pending_apk():
    assert scan_mod.parse_pending(APK_LIST, "apk") == ["musl", "busybox"]


def test_parse_pending_empty():
    for mgr in ("apt", "dnf", "apk"):
        assert scan_mod.parse_pending("", mgr) == []


def test_parse_pending_apt_noop_run():
    out = "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"
    assert scan_mod.parse_pending(out, "apt") == []


# --- scan_cmd ------------------------------------------------------------------

def test_scan_cmd_is_read_only():
    assert "-s dist-upgrade" in scan_mod.scan_cmd("apt")
    assert "check-update" in scan_mod.scan_cmd("dnf")
    assert "version -l" in scan_mod.scan_cmd("apk")
    # No mutating verbs.
    for mgr in ("apt", "dnf", "apk"):
        cmd = scan_mod.scan_cmd(mgr)
        assert " -y" not in cmd
        assert "upgrade -y" not in cmd


# --- scan_host -----------------------------------------------------------------

def test_scan_host_apt():
    ex = ScriptedExecutor(script={
        "which apt-get": [_ok("/usr/bin/apt-get\napt")],
        "dist-upgrade": [_ok(APT_SIM)],
    })
    result = scan_mod.scan_host(ex)
    assert result["pkg_mgr"] == "apt"
    assert result["pending_count"] == 2
    assert result["pending"] == ["libssl3", "curl"]
    assert result["error"] is None


def test_scan_host_dnf_rc100_tolerated():
    ex = ScriptedExecutor(script={
        "which apt-get": [_ok("dnf")],
        "check-update": [PrimitiveResult(rc=100, failed=True, stdout=DNF_CHECK)],
    })
    result = scan_mod.scan_host(ex)
    assert result["pkg_mgr"] == "dnf"
    assert result["pending_count"] == 2
    assert result["error"] is None


def test_scan_host_error_captured():
    ex = ScriptedExecutor(script={
        "which apt-get": [_ok("apt")],
        "dist-upgrade": [_fail(stderr="no network")],
    })
    result = scan_mod.scan_host(ex)
    assert result["error"] is not None and "no network" in result["error"]
    assert result["pending_count"] == 0


# --- scan_lxc ------------------------------------------------------------------

_INTROSPECT_RUNNING = {
    "config_stdout": "hostname: sonarr\nostype: debian\n",
    "status_stdout": "status: running",
    "pull_rc": 0,
    "script_stdout": 'bash -c "$(wget -qLO - https://github.com/community-scripts/ProxmoxVE/raw/main/ct/sonarr.sh)"',
}


def _patch_github(monkeypatch, *, latest="v4.1.0"):
    ct_script = 'check_for_gh_release "sonarr" "Sonarr/Sonarr"'
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: type("R", (), {"body": ct_script})())
    monkeypatch.setattr(http_mod, "get_json",
                        lambda url, **kw: {"tag_name": latest})


def test_scan_lxc_running_with_app(monkeypatch):
    _patch_github(monkeypatch)
    ex = ScriptedExecutor(
        script={
            "dist-upgrade": [_ok(APT_SIM)],
            "cat ~/.sonarr": [_ok("4.0.17")],
        },
        introspect_facts=_INTROSPECT_RUNNING,
    )
    result = scan_mod.scan_lxc(ex, "101", "pve-01")
    assert result["name"] == "sonarr"
    assert result["id"] == "101"
    assert result["os_pending_count"] == 2
    assert result["app"] == {"script": "sonarr", "current": "4.0.17",
                             "latest": "v4.1.0", "outdated": True}
    assert result["error"] is None


def test_scan_lxc_up_to_date_not_outdated(monkeypatch):
    _patch_github(monkeypatch, latest="v4.0.17")
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok("")], "cat ~/.sonarr": [_ok("4.0.17")]},
        introspect_facts=_INTROSPECT_RUNNING,
    )
    result = scan_mod.scan_lxc(ex, "101", "pve-01")
    assert result["app"]["outdated"] is False


def test_scan_lxc_stopped_skipped():
    ex = ScriptedExecutor(introspect_facts={
        "config_stdout": "hostname: sonarr\nostype: debian\n",
        "status_stdout": "status: stopped",
        "pull_rc": 0, "script_stdout": "",
    })
    result = scan_mod.scan_lxc(ex, "101", "pve-01")
    assert result["skipped"] == "stopped"
    assert ex.commands == []                # nothing executed inside the CT


def test_scan_lxc_template_skipped():
    ex = ScriptedExecutor(introspect_facts={
        "config_stdout": "hostname: tpl\nostype: debian\ntemplate: 1\n",
        "status_stdout": "status: stopped",
        "pull_rc": 0, "script_stdout": "",
    })
    result = scan_mod.scan_lxc(ex, "101", "pve-01")
    assert result["skipped"] == "template"


def test_scan_lxc_no_update_script_app_none():
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok("")]},
        introspect_facts={
            "config_stdout": "hostname: plain\nostype: debian\n",
            "status_stdout": "status: running",
            "pull_rc": 1, "script_stdout": "",
        },
    )
    result = scan_mod.scan_lxc(ex, "105", "pve-01")
    assert result["app"] is None
    assert result["error"] is None


def test_scan_lxc_alpine_uses_ash_and_apk():
    ex = ScriptedExecutor(
        script={"apk version": [_ok(APK_LIST)]},
        introspect_facts={
            "config_stdout": "hostname: alp\nostype: alpine\n",
            "status_stdout": "status: running",
            "pull_rc": 1, "script_stdout": "",
        },
    )
    result = scan_mod.scan_lxc(ex, "107", "pve-01")
    assert result["os_pending_count"] == 2
    pct_cmds = [c for c in ex.commands if c.startswith("pct exec")]
    assert pct_cmds and " ash -c" in pct_cmds[0]


# --- write_pending --------------------------------------------------------------

def test_write_pending_files_and_prune(tmp_path):
    for i in range(5):
        scan_mod.write_pending({"timestamp": f"2026010{i}T000000000000Z", "hosts": {},
                                "lxc": {}}, history_dir=tmp_path, keep=3)
    files = sorted(p.name for p in tmp_path.glob("pending-*.json"))
    assert "pending-latest.json" in files
    timestamped = [f for f in files if f != "pending-latest.json"]
    assert timestamped == [
        "pending-20260102T000000000000Z.json",
        "pending-20260103T000000000000Z.json",
        "pending-20260104T000000000000Z.json",
    ]
    latest = json.loads((tmp_path / "pending-latest.json").read_text())
    assert latest["timestamp"] == "20260104T000000000000Z"


# --- run_fleet_scan ---------------------------------------------------------------

def _patch_executors(monkeypatch, factory):
    import proxmox_fleet.executor as executor_mod
    monkeypatch.setattr(executor_mod, "RunnerExecutor", factory)


def test_run_fleet_scan_end_to_end(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\n"
        "[remote_hosts]\nweb-01 ansible_host=10.0.2.1\n"
        "[proxmox_vms]\n[custom_hosts]\n"
    )
    _patch_github(monkeypatch)

    def _factory(host, **kw):
        if host == "pve-01":
            return ScriptedExecutor(
                script={
                    "pct list": [_ok("101\n")],
                    "which apt-get": [_ok("apt")],
                    "dist-upgrade": [_ok(APT_SIM), _ok(APT_SIM)],
                    "cat ~/.sonarr": [_ok("4.0.17")],
                },
                introspect_facts=_INTROSPECT_RUNNING,
            )
        return ScriptedExecutor(script={
            "which apt-get": [_ok("apt")],
            "dist-upgrade": [_ok(APT_SIM)],
        })

    _patch_executors(monkeypatch, _factory)
    monkeypatch.setattr(scan_mod, "_discover_lxcs", lambda ex, s: ["101"])

    settings = GlobalSettings(fleet_history_dir=str(tmp_path / "hist"))
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv))

    assert rc == 0
    latest = json.loads((tmp_path / "hist" / "pending-latest.json").read_text())
    assert latest["hosts"]["web-01"]["pending_count"] == 2
    assert latest["hosts"]["web-01"]["kind"] == "remote"
    assert latest["hosts"]["pve-01"]["kind"] == "node"
    assert latest["lxc"]["pve-01/101"]["name"] == "sonarr"
    assert latest["lxc"]["pve-01/101"]["id"] == "101"
    assert latest["lxc"]["pve-01/101"]["app"]["outdated"] is True


def test_run_fleet_scan_same_id_on_two_clusters_keeps_both(tmp_path, monkeypatch):
    """Two clusters' LXC 101 must not overwrite each other in the snapshot."""
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[proxmox_nodes]\n"
        "alpha-01 ansible_host=10.0.0.1 cluster=alpha\n"
        "beta-01 ansible_host=10.1.0.1 cluster=beta\n"
        "[remote_hosts]\n[proxmox_vms]\n[custom_hosts]\n"
    )
    _patch_github(monkeypatch)

    def _factory(host, **kw):
        return ScriptedExecutor(
            script={
                "which apt-get": [_ok("apt")],
                "dist-upgrade": [_ok(APT_SIM), _ok(APT_SIM)],
                "cat ~/.sonarr": [_ok("4.0.17")],
            },
            introspect_facts=_INTROSPECT_RUNNING,
        )

    _patch_executors(monkeypatch, _factory)
    monkeypatch.setattr(scan_mod, "_discover_lxcs", lambda ex, s: ["101"])

    settings = GlobalSettings(fleet_history_dir=str(tmp_path / "hist"))
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv))

    assert rc == 0
    latest = json.loads((tmp_path / "hist" / "pending-latest.json").read_text())
    assert set(latest["lxc"]) == {"alpha-01/101", "beta-01/101"}
    assert latest["lxc"]["alpha-01/101"]["node"] == "alpha-01"
    assert latest["lxc"]["beta-01/101"]["node"] == "beta-01"


def test_run_fleet_scan_limit(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\n"
        "[remote_hosts]\nweb-01\nweb-02\n"
        "[proxmox_vms]\n[custom_hosts]\n"
    )
    scanned: List[str] = []

    def _factory(host, **kw):
        scanned.append(host)
        return ScriptedExecutor(script={"which apt-get": [_ok("apt")],
                                        "dist-upgrade": [_ok("")]})

    _patch_executors(monkeypatch, _factory)
    settings = GlobalSettings(fleet_history_enabled=False)
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv),
                                 limit={"web-02"})
    assert rc == 0
    assert scanned == ["web-02"]            # node skipped (not in limit, no ids)


def test_run_fleet_scan_error_sets_exit_code(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text("[remote_hosts]\nweb-01\n[proxmox_nodes]\n[proxmox_vms]\n[custom_hosts]\n")

    def _factory(host, **kw):
        return ScriptedExecutor(script={"which apt-get": [_ok("apt")],
                                        "dist-upgrade": [_fail(stderr="down")]})

    _patch_executors(monkeypatch, _factory)
    settings = GlobalSettings(fleet_history_enabled=False)
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv))
    assert rc == 1


# --- pending_summary / read_pending (readers) ------------------------------------

def _write_scan(tmp_path, *, ts, hosts=None, lxc=None):
    scan_mod.write_pending({"timestamp": ts, "hosts": hosts or {}, "lxc": lxc or {}},
                           history_dir=tmp_path, keep=0)


def test_pending_summary_newest_first_with_aggregates(tmp_path):
    _write_scan(tmp_path, ts="20260101T000000000000Z")
    _write_scan(
        tmp_path, ts="20260102T000000000000Z",
        hosts={"web-01": {"kind": "remote", "pkg_mgr": "apt", "pending_count": 3,
                          "pending": ["a", "b", "c"], "error": None},
               "pve-01": {"kind": "node", "pkg_mgr": "apt", "pending_count": 1,
                          "pending": ["d"], "error": "ssh down"}},
        lxc={"101": {"node": "pve-01", "name": "sonarr", "skipped": None,
                     "os_pending_count": 2, "os_pending": ["x", "y"],
                     "app": {"script": "sonarr", "current": "1", "latest": "2",
                             "outdated": True}, "error": None}},
    )
    rows = scan_mod.pending_summary(tmp_path)
    assert [r["timestamp"] for r in rows] == ["20260102T000000000000Z",
                                              "20260101T000000000000Z"]
    newest = rows[0]
    assert newest["hosts_pending"] == 4
    assert newest["lxc_os_pending"] == 2
    assert newest["outdated_apps"] == 1
    assert newest["errors"] == 1


def test_pending_summary_excludes_latest_and_limit(tmp_path):
    for i in range(4):
        _write_scan(tmp_path, ts=f"2026010{i}T000000000000Z")
    rows = scan_mod.pending_summary(tmp_path, limit=2)
    assert len(rows) == 2
    # limit <= 0 → all timestamped scans, latest.json never double-counted.
    assert len(scan_mod.pending_summary(tmp_path, limit=0)) == 4


def test_pending_summary_skips_corrupt(tmp_path):
    _write_scan(tmp_path, ts="20260101T000000000000Z")
    (tmp_path / "pending-20260102T000000000000Z.json").write_text("{nope", encoding="utf-8")
    rows = scan_mod.pending_summary(tmp_path)
    assert [r["timestamp"] for r in rows] == ["20260101T000000000000Z"]


def test_pending_summary_empty_dir(tmp_path):
    assert scan_mod.pending_summary(tmp_path) == []
    assert scan_mod.pending_summary(tmp_path / "missing") == []


def test_read_pending_ref_forms(tmp_path):
    _write_scan(tmp_path, ts="20260101T000000000000Z")
    latest = scan_mod.read_pending(tmp_path, "latest")
    bare = scan_mod.read_pending(tmp_path, "20260101T000000000000Z")
    prefixed = scan_mod.read_pending(tmp_path, "pending-20260101T000000000000Z")
    filename = scan_mod.read_pending(tmp_path, "pending-20260101T000000000000Z.json")
    assert latest == bare == prefixed == filename
    assert latest["timestamp"] == "20260101T000000000000Z"


def test_read_pending_missing_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        scan_mod.read_pending(tmp_path, "latest")


# --- scan health signals (disk / OS target) -------------------------------------

_DF_90 = ("Filesystem     1024-blocks    Used Available Capacity Mounted on\n"
          "/dev/rbd17         4046560 3416796    403668      90% /\n")
_OSREL_BOOKWORM = 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nVERSION_ID="12"\nID=debian\n'
_OSREL_TRIXIE = 'VERSION_ID="13"\nID=debian\n'


def _patch_github_trixie(monkeypatch, *, latest="v4.1.0"):
    """ct script that both names a GH repo and targets debian 13."""
    ct_script = ('var_os="${var_os:-debian}"\n'
                 'var_version="${var_version:-13}"\n'
                 'check_for_gh_release "sonarr" "Sonarr/Sonarr"\n')
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: type("R", (), {"body": ct_script})())
    monkeypatch.setattr(http_mod, "get_json", lambda url, **kw: {"tag_name": latest})


def test_scan_lxc_reports_disk_and_os(monkeypatch):
    _patch_github_trixie(monkeypatch)
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok(APT_SIM)], "cat ~/.sonarr": [_ok("4.0.17")]},
        introspect_facts=dict(_INTROSPECT_RUNNING,
                              df_stdout=_DF_90, os_release_stdout=_OSREL_TRIXIE),
    )
    result = scan_mod.scan_lxc(ex, "130", "pve-01")
    assert result["disk_percent"] == 90
    assert result["os"] == "debian 13"
    assert result["os_mismatch"] is None


def test_scan_lxc_flags_os_mismatch(monkeypatch):
    _patch_github_trixie(monkeypatch)
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok("")], "cat ~/.sonarr": [_ok("4.0.17")]},
        introspect_facts=dict(_INTROSPECT_RUNNING,
                              df_stdout=_DF_90, os_release_stdout=_OSREL_BOOKWORM),
    )
    result = scan_mod.scan_lxc(ex, "123", "pve-01")
    assert result["os"] == "debian 12"
    assert "debian 12" in result["os_mismatch"]
    assert "debian 13" in result["os_mismatch"]


def test_scan_lxc_health_absent_without_introspect_facts(monkeypatch):
    """Older primitive output (no df/os-release facts) must not break the scan."""
    _patch_github(monkeypatch)
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok("")], "cat ~/.sonarr": [_ok("4.0.17")]},
        introspect_facts=_INTROSPECT_RUNNING,
    )
    result = scan_mod.scan_lxc(ex, "101", "pve-01")
    assert result["disk_percent"] is None
    assert result["os"] == ""
    assert result["os_mismatch"] is None


def test_pending_summary_counts_health_signals(tmp_path):
    scan = {
        "timestamp": "2026-07-23T04-00-00Z",
        "hosts": {},
        "lxc": {
            "123": {"os_pending_count": 0, "app": None, "disk_percent": 63,
                    "os_mismatch": "container runs debian 12 but ...", "error": None},
            "130": {"os_pending_count": 4, "app": None, "disk_percent": 90,
                    "os_mismatch": None, "error": None},
            "105": {"os_pending_count": 0, "app": None, "disk_percent": 52,
                    "os_mismatch": None, "error": None},
        },
    }
    (tmp_path / "pending-2026-07-23T04-00-00Z.json").write_text(json.dumps(scan))
    rows = scan_mod.pending_summary(tmp_path, disk_threshold=75)
    assert rows[0]["low_disk"] == 1        # only 130 at 90%
    assert rows[0]["os_mismatch"] == 1     # only 123


def test_pending_summary_tolerates_scans_without_health_keys(tmp_path):
    scan = {
        "timestamp": "2026-07-01T04-00-00Z",
        "hosts": {},
        "lxc": {"101": {"os_pending_count": 2, "app": None, "error": None}},
    }
    (tmp_path / "pending-2026-07-01T04-00-00Z.json").write_text(json.dumps(scan))
    rows = scan_mod.pending_summary(tmp_path)
    assert rows[0]["low_disk"] == 0
    assert rows[0]["os_mismatch"] == 0


def test_scan_lxc_returns_both_the_cluster_id_and_the_health_signals(monkeypatch):
    """scan_lxc's result dict is the union of #38's `id` and #41's health keys."""
    _patch_github_trixie(monkeypatch)
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok(APT_SIM)], "cat ~/.sonarr": [_ok("4.0.17")]},
        introspect_facts=dict(_INTROSPECT_RUNNING,
                              df_stdout=_DF_90, os_release_stdout=_OSREL_BOOKWORM),
    )
    result = scan_mod.scan_lxc(ex, "123", "Hammond")
    assert result["id"] == "123"
    assert result["node"] == "Hammond"
    assert result["disk_percent"] == 90
    assert result["os"] == "debian 12"
    assert result["os_mismatch"] is not None
