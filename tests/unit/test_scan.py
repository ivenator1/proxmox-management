"""Tests for proxmox_fleet.scan — the read-only pending-updates walk.

Parsers are tested directly; scan_host/scan_lxc/run_fleet_scan run against
scripted executors (no Ansible/PVE), with GitHub HTTP monkeypatched.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List

import pytest

from proxmox_fleet import http as http_mod
from proxmox_fleet import scan as scan_mod
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult, UnreachableHostError


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

# PR2 sentinel-delimited scan outputs — the same table the plain fixtures
# carry, plus the __FLEET_META__ tail (reboot marker + /etc/os-release) and,
# for dnf, the __FLEET_SEC__ section between the two check-update runs.
APT_SCAN = (
    APT_SIM
    + """\
__FLEET_META__
reboot_required
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
VERSION_ID="12"
ID=debian
"""
)

DNF_SCAN = (
    DNF_CHECK
    + """\
__FLEET_SEC__
openssl.x86_64       1:3.0.7-27.el9        appstream
__FLEET_META__
reboot_required
PRETTY_NAME="Rocky Linux 9.3 (Blue Onyx)"
ID="rocky"
VERSION_ID="9.3"
"""
)

APK_SCAN = (
    APK_LIST
    + """\
__FLEET_META__
PRETTY_NAME="Alpine Linux v3.19"
ID=alpine
VERSION_ID=3.19.1
"""
)


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


def test_scan_cmd_has_no_single_quotes():
    """Regression for bug #2: scan_cmd rides inside `pct exec <id> -- <shell>
    -c '<cmd>'`, so an embedded single quote breaks the Alpine LXC scan."""
    for mgr in ("apt", "dnf", "apk"):
        assert "'" not in scan_mod.scan_cmd(mgr), f"single quote in {mgr} cmd"


def test_scan_cmd_apk_uses_double_quoted_less_than():
    cmd = scan_mod.scan_cmd("apk")
    assert 'version -l "<"' in cmd  # "<" not '<'


def test_scan_cmd_preserves_primary_rc():
    """The metadata tail must not swallow the scan's real exit code: rc is
    captured right after the scan section and re-raised via `exit $rc`."""
    for mgr in ("apt", "dnf", "apk"):
        cmd = scan_mod.scan_cmd(mgr)
        assert "rc=$?" in cmd
        assert "exit $rc" in cmd
    # dnf's rc must come from the *primary* check-update — before the sentinel
    # that introduces the --security run (which also exits 100 when it finds
    # updates, and must not become the command's rc).
    dnf = scan_mod.scan_cmd("dnf")
    assert dnf.index("rc=$") < dnf.index("__FLEET_SEC__")


def test_scan_cmd_sentinel_sections():
    apt = scan_mod.scan_cmd("apt")
    assert apt.index("__FLEET_META__") > apt.index("dist-upgrade")
    assert "__FLEET_SEC__" not in apt
    dnf = scan_mod.scan_cmd("dnf")
    assert dnf.index("__FLEET_SEC__") < dnf.index("__FLEET_META__")
    assert "check-update --security" in dnf
    apk = scan_mod.scan_cmd("apk")
    assert "__FLEET_SEC__" not in apk
    assert "__FLEET_META__" in apk
    # apk emits no reboot check — Alpine has no such concept.
    assert "reboot-required" not in apk


# --- parse_os_release (scan-local) --------------------------------------------


def test_parse_os_release_quoted_and_bare_values():
    assert scan_mod.parse_os_release(_OSREL_BOOKWORM) == {
        "id": "debian",
        "version_id": "12",
        "pretty_name": "Debian GNU/Linux 12 (bookworm)",
    }
    bare = "ID=ubuntu\nVERSION_ID=22.04\nPRETTY_NAME=Ubuntu 22.04.3 LTS\n"
    assert scan_mod.parse_os_release(bare) == {
        "id": "ubuntu",
        "version_id": "22.04",
        "pretty_name": "Ubuntu 22.04.3 LTS",
    }


def test_parse_os_release_missing_fields_are_empty():
    assert scan_mod.parse_os_release("ID=debian\n") == {"id": "debian", "version_id": "", "pretty_name": ""}
    assert scan_mod.parse_os_release("") == {"id": "", "version_id": "", "pretty_name": ""}


# --- parse_scan_output ---------------------------------------------------------


def test_parse_scan_output_apt_sections():
    out = scan_mod.parse_scan_output(APT_SCAN, "apt")
    assert out["pending"] == ["libssl3", "curl"]
    assert out["security"] == ["curl"]  # the Debian-Security archive
    assert out["reboot_required"] is True
    assert out["os_release"] == {"id": "debian", "version_id": "12", "pretty_name": "Debian GNU/Linux 12 (bookworm)"}


def test_parse_scan_output_apt_no_reboot():
    out = scan_mod.parse_scan_output(APT_SIM + "__FLEET_META__\nID=debian\nVERSION_ID=12\n", "apt")
    assert out["pending"] == ["libssl3", "curl"]
    assert out["reboot_required"] is False


def test_parse_scan_output_dnf_sections():
    out = scan_mod.parse_scan_output(DNF_SCAN, "dnf")
    assert out["pending"] == ["kernel", "openssl"]
    assert out["security"] == ["openssl"]
    assert out["reboot_required"] is True
    assert out["os_release"] == {"id": "rocky", "version_id": "9.3", "pretty_name": "Rocky Linux 9.3 (Blue Onyx)"}


def test_parse_scan_output_apk_security_empty_no_reboot():
    out = scan_mod.parse_scan_output(APK_SCAN, "apk")
    assert out["pending"] == ["musl", "busybox"]
    assert out["security"] == []
    assert out["reboot_required"] is False
    assert out["os_release"] == {"id": "alpine", "version_id": "3.19.1", "pretty_name": "Alpine Linux v3.19"}


def test_parse_scan_output_missing_sections_degrade():
    """Older commands / partial captures have no sentinels: the whole output
    is pending, security defaults per manager, no reboot, empty os_release."""
    apt = scan_mod.parse_scan_output(APT_SIM, "apt")
    assert apt["pending"] == ["libssl3", "curl"]
    assert apt["security"] == ["curl"]  # archive still detectable
    assert apt["reboot_required"] is False
    assert apt["os_release"] == {"id": "", "version_id": "", "pretty_name": ""}
    dnf = scan_mod.parse_scan_output(DNF_CHECK, "dnf")
    assert dnf["pending"] == ["kernel", "openssl"]
    assert dnf["security"] == []
    apk = scan_mod.parse_scan_output(APK_LIST, "apk")
    assert apk["pending"] == ["musl", "busybox"]
    assert apk["security"] == []
    assert scan_mod.parse_scan_output("", "apt") == {
        "pending": [],
        "security": [],
        "reboot_required": False,
        "os_release": {"id": "", "version_id": "", "pretty_name": ""},
    }


def test_parse_scan_output_dnf_without_security_sentinel():
    """A dnf output that reached META but never emitted __FLEET_SEC__: pending
    is everything before META, security [] (unknowable), reboot still parsed."""
    out = scan_mod.parse_scan_output(DNF_CHECK + "__FLEET_META__\nreboot_required\nID=rocky\n", "dnf")
    assert out["pending"] == ["kernel", "openssl"]
    assert out["security"] == []
    assert out["reboot_required"] is True


# --- scan_host -----------------------------------------------------------------


def test_scan_host_apt():
    ex = ScriptedExecutor(
        script={
            "which apt-get": [_ok("/usr/bin/apt-get\napt")],
            "dist-upgrade": [_ok(APT_SIM)],
        }
    )
    result = scan_mod.scan_host(ex)
    assert result["pkg_mgr"] == "apt"
    assert result["pending_count"] == 2
    assert result["pending"] == ["libssl3", "curl"]
    assert result["error"] is None


def test_scan_host_dnf_rc100_tolerated():
    ex = ScriptedExecutor(
        script={
            "which apt-get": [_ok("dnf")],
            "check-update": [PrimitiveResult(rc=100, failed=True, stdout=DNF_CHECK)],
        }
    )
    result = scan_mod.scan_host(ex)
    assert result["pkg_mgr"] == "dnf"
    assert result["pending_count"] == 2
    assert result["error"] is None


def test_scan_host_error_captured():
    ex = ScriptedExecutor(
        script={
            "which apt-get": [_ok("apt")],
            "dist-upgrade": [_fail(stderr="no network")],
        }
    )
    result = scan_mod.scan_host(ex)
    assert result["error"] is not None and "no network" in result["error"]
    assert result["pending_count"] == 0


def test_scan_host_dnf_non_100_error():
    """dnf rc=100 means "updates exist" (tolerated); any other failure rc is a
    real error and must land in the entry's error key."""
    ex = ScriptedExecutor(
        script={
            "which apt-get": [_ok("dnf")],
            "check-update": [_fail(rc=5, stderr="metadata download failed")],
        }
    )
    result = scan_mod.scan_host(ex)
    assert "rc=5" in result["error"]
    assert result["pending_count"] == 0


def test_scan_host_apt_security_reboot_os_release():
    ex = ScriptedExecutor(
        script={
            "which apt-get": [_ok("/usr/bin/apt-get\napt")],
            "dist-upgrade": [_ok(APT_SCAN)],
        }
    )
    result = scan_mod.scan_host(ex)
    assert result["pkg_mgr"] == "apt"
    assert result["pending"] == ["libssl3", "curl"]
    assert result["security"] == ["curl"]
    assert result["security_count"] == 1
    assert result["reboot_required"] is True
    assert result["os_release"] == {"id": "debian", "version_id": "12", "pretty_name": "Debian GNU/Linux 12 (bookworm)"}
    assert result["error"] is None


def test_scan_host_apt_no_reboot_no_security():
    ex = ScriptedExecutor(
        script={
            "which apt-get": [_ok("apt")],
            "dist-upgrade": [_ok(APT_SIM + "__FLEET_META__\nID=debian\n")],
        }
    )
    result = scan_mod.scan_host(ex)
    assert result["security"] == ["curl"]  # still detected from Inst lines
    assert result["reboot_required"] is False


def test_scan_host_dnf_rc100_tolerated_with_security():
    ex = ScriptedExecutor(
        script={
            "which apt-get": [_ok("dnf")],
            "check-update": [PrimitiveResult(rc=100, failed=True, stdout=DNF_SCAN)],
        }
    )
    result = scan_mod.scan_host(ex)
    assert result["pending"] == ["kernel", "openssl"]
    assert result["security"] == ["openssl"]
    assert result["security_count"] == 1
    assert result["reboot_required"] is True
    assert result["error"] is None


def test_scan_host_error_path_keeps_full_schema():
    """The error path must not lose the PR2 keys — readers rely on them."""
    ex = ScriptedExecutor(
        script={
            "which apt-get": [_ok("apt")],
            "dist-upgrade": [_fail(stderr="no network")],
        }
    )
    result = scan_mod.scan_host(ex)
    assert result["error"]
    assert result["security_count"] == 0
    assert result["security"] == []
    assert result["reboot_required"] is False
    assert result["os_release"] == {"id": "", "version_id": "", "pretty_name": ""}


# --- scan_lxc ------------------------------------------------------------------

_INTROSPECT_RUNNING = {
    "config_stdout": "hostname: sonarr\nostype: debian\n",
    "status_stdout": "status: running",
    "pull_rc": 0,
    "script_stdout": 'bash -c "$(wget -qLO - https://github.com/community-scripts/ProxmoxVE/raw/main/ct/sonarr.sh)"',
}

# A dnf host (fedora → pkg_mgr_for_ostype fallback). pull_rc=1 short-circuits
# _lxc_app_pending before any GitHub HTTP, so the dnf-rc tests need no monkeypatch.
_INTROSPECT_DNF = {
    "config_stdout": "hostname: rocksrv\nostype: fedora\n",
    "status_stdout": "status: running",
    "pull_rc": 1,
    "script_stdout": "",
}


def _patch_github(monkeypatch, *, latest="v4.1.0"):
    ct_script = 'check_for_gh_release "sonarr" "Sonarr/Sonarr"'
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: type("R", (), {"body": ct_script})())
    monkeypatch.setattr(http_mod, "get_json", lambda url, **kw: {"tag_name": latest})


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
    assert result["app"] == {"script": "sonarr", "current": "4.0.17", "latest": "v4.1.0", "outdated": True}
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
    ex = ScriptedExecutor(
        introspect_facts={
            "config_stdout": "hostname: sonarr\nostype: debian\n",
            "status_stdout": "status: stopped",
            "pull_rc": 0,
            "script_stdout": "",
        }
    )
    result = scan_mod.scan_lxc(ex, "101", "pve-01")
    assert result["skipped"] == "stopped"
    assert ex.commands == []  # nothing executed inside the CT


def test_scan_lxc_template_skipped():
    ex = ScriptedExecutor(
        introspect_facts={
            "config_stdout": "hostname: tpl\nostype: debian\ntemplate: 1\n",
            "status_stdout": "status: stopped",
            "pull_rc": 0,
            "script_stdout": "",
        }
    )
    result = scan_mod.scan_lxc(ex, "101", "pve-01")
    assert result["skipped"] == "template"


def test_scan_lxc_no_update_script_app_none():
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok("")]},
        introspect_facts={
            "config_stdout": "hostname: plain\nostype: debian\n",
            "status_stdout": "status: running",
            "pull_rc": 1,
            "script_stdout": "",
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
            "pull_rc": 1,
            "script_stdout": "",
        },
    )
    result = scan_mod.scan_lxc(ex, "107", "pve-01")
    assert result["os_pending_count"] == 2
    pct_cmds = [c for c in ex.commands if c.startswith("pct exec")]
    assert pct_cmds and " ash -c" in pct_cmds[0]


def test_scan_lxc_security_reboot_and_os_release(monkeypatch):
    _patch_github(monkeypatch)
    ex = ScriptedExecutor(
        script={
            "dist-upgrade": [_ok(APT_SCAN)],
            "cat ~/.sonarr": [_ok("4.0.17")],
        },
        introspect_facts=dict(_INTROSPECT_RUNNING, os_release_stdout=_OSREL_BOOKWORM),
    )
    result = scan_mod.scan_lxc(ex, "101", "pve-01")
    assert result["os_pending_count"] == 2
    assert result["os_security"] == ["curl"]
    assert result["os_security_count"] == 1
    assert result["reboot_required"] is True
    # os_release comes from the introspect pass (no extra command), keyed like
    # the host entries.
    assert result["os_release"] == {"id": "debian", "version_id": "12", "pretty_name": "Debian GNU/Linux 12 (bookworm)"}
    assert result["os"] == "debian 12"


def test_scan_lxc_legacy_output_without_sentinels(monkeypatch):
    """Old-style scan output (no sentinels) must not break the scan: pending
    still parses, apt security still detected from Inst archives, no reboot,
    empty os_release."""
    _patch_github(monkeypatch)
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok(APT_SIM)], "cat ~/.sonarr": [_ok("4.0.17")]},
        introspect_facts=_INTROSPECT_RUNNING,
    )
    result = scan_mod.scan_lxc(ex, "101", "pve-01")
    assert result["os_pending_count"] == 2
    assert result["os_security_count"] == 1
    assert result["reboot_required"] is False
    assert result["os_release"] == {"id": "", "version_id": "", "pretty_name": ""}
    assert result["error"] is None


def test_scan_lxc_alpine_apk_security_empty(monkeypatch):
    _patch_github(monkeypatch)
    ex = ScriptedExecutor(
        script={"apk version": [_ok(APK_SCAN)]},
        introspect_facts={
            "config_stdout": "hostname: alp\nostype: alpine\n",
            "status_stdout": "status: running",
            "pull_rc": 1,
            "script_stdout": "",
            "os_release_stdout": 'PRETTY_NAME="Alpine Linux v3.19"\nID=alpine\nVERSION_ID=3.19.1\n',
        },
    )
    result = scan_mod.scan_lxc(ex, "107", "pve-01")
    assert result["os_pending_count"] == 2
    assert result["os_security"] == []
    assert result["reboot_required"] is False
    assert result["os_release"] == {"id": "alpine", "version_id": "3.19.1", "pretty_name": "Alpine Linux v3.19"}


def test_scan_lxc_dnf_rc100_tolerated():
    """dnf check-update exits 100 when anything is pending — the scan must not
    record an error, and the pending table still parses."""
    ex = ScriptedExecutor(
        script={"check-update": [PrimitiveResult(rc=100, failed=True, stdout=DNF_SCAN)]},
        introspect_facts=_INTROSPECT_DNF,
    )
    result = scan_mod.scan_lxc(ex, "108", "pve-01")
    assert result["error"] is None
    assert result["os_pending_count"] == 2
    assert result["os_pending"] == ["kernel", "openssl"]


def test_scan_lxc_dnf_non_100_error():
    """A dnf failure rc other than 100 is a real error, not a pending signal."""
    ex = ScriptedExecutor(
        script={"check-update": [_fail(rc=5, stderr="metadata download failed")]},
        introspect_facts=_INTROSPECT_DNF,
    )
    result = scan_mod.scan_lxc(ex, "108", "pve-01")
    assert "rc=5" in result["error"]
    assert result["os_pending_count"] == 0


def test_scan_lxc_apt_failed_records_error():
    """A failed apt simulate is an error — apt has no rc=100 convention."""
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_fail(rc=1, stderr="lock held")]},
        introspect_facts=_INTROSPECT_RUNNING,
    )
    result = scan_mod.scan_lxc(ex, "101", "pve-01")
    assert "lock held" in result["error"]
    assert result["os_pending_count"] == 0


def test_scan_lxc_apk_failed_records_error():
    """A failed apk scan is an error — apk has no rc=100 convention."""
    ex = ScriptedExecutor(
        script={"apk version": [_fail(rc=3, stderr="permission denied")]},
        introspect_facts={
            "config_stdout": "hostname: alp\nostype: alpine\n",
            "status_stdout": "status: running",
            "pull_rc": 1,
            "script_stdout": "",
        },
    )
    result = scan_mod.scan_lxc(ex, "107", "pve-01")
    assert "rc=3" in result["error"]
    assert result["os_pending_count"] == 0


# --- write_pending --------------------------------------------------------------


def test_write_pending_files_and_prune(tmp_path):
    for i in range(5):
        scan_mod.write_pending(
            {"timestamp": f"2026010{i}T000000000000Z", "hosts": {}, "lxc": {}}, history_dir=tmp_path, keep=3
        )
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
        return ScriptedExecutor(
            script={
                "which apt-get": [_ok("apt")],
                "dist-upgrade": [_ok(APT_SIM)],
            }
        )

    _patch_executors(monkeypatch, _factory)
    monkeypatch.setattr(scan_mod, "_discover_lxcs", lambda ex, s, **kw: ["101"])

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
    monkeypatch.setattr(scan_mod, "_discover_lxcs", lambda ex, s, **kw: ["101"])

    settings = GlobalSettings(fleet_history_dir=str(tmp_path / "hist"))
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv))

    assert rc == 0
    latest = json.loads((tmp_path / "hist" / "pending-latest.json").read_text())
    assert set(latest["lxc"]) == {"alpha-01/101", "beta-01/101"}
    assert latest["lxc"]["alpha-01/101"]["node"] == "alpha-01"
    assert latest["lxc"]["beta-01/101"]["node"] == "beta-01"


def test_run_fleet_scan_metadata_lands_on_both_cluster_entries(tmp_path, monkeypatch):
    """PR2: security/reboot/os_release metadata is per-node — each cluster's
    LXC entry and node host carry their own, keyed node/id like everything
    else in a multi-cluster snapshot."""
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
                "dist-upgrade": [_ok(APT_SCAN), _ok(APT_SCAN)],
                "cat ~/.sonarr": [_ok("4.0.17")],
            },
            introspect_facts=dict(_INTROSPECT_RUNNING, os_release_stdout=_OSREL_BOOKWORM),
        )

    _patch_executors(monkeypatch, _factory)
    monkeypatch.setattr(scan_mod, "_discover_lxcs", lambda ex, s, **kw: ["101"])

    settings = GlobalSettings(fleet_history_dir=str(tmp_path / "hist"))
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv))
    assert rc == 0
    latest = json.loads((tmp_path / "hist" / "pending-latest.json").read_text())
    for key in ("alpha-01/101", "beta-01/101"):
        entry = latest["lxc"][key]
        assert entry["os_security"] == ["curl"]
        assert entry["os_security_count"] == 1
        assert entry["reboot_required"] is True
        assert entry["os_release"]["pretty_name"] == "Debian GNU/Linux 12 (bookworm)"
    for name in ("alpha-01", "beta-01"):
        host = latest["hosts"][name]
        assert host["security_count"] == 1
        assert host["reboot_required"] is True
        assert host["os_release"]["id"] == "debian"


def test_run_fleet_scan_limit(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\n[remote_hosts]\nweb-01\nweb-02\n[proxmox_vms]\n[custom_hosts]\n"
    )
    scanned: List[str] = []

    def _factory(host, **kw):
        scanned.append(host)
        return ScriptedExecutor(script={"which apt-get": [_ok("apt")], "dist-upgrade": [_ok("")]})

    _patch_executors(monkeypatch, _factory)
    settings = GlobalSettings(fleet_history_enabled=False)
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv), limit={"web-02"})
    assert rc == 0
    assert scanned == ["web-02"]  # node skipped (not in limit, no ids)


def test_run_fleet_scan_qualified_vm_limit_selects_only_that_cluster(tmp_path, monkeypatch):
    """--scan --limit alpha/200 scans only alpha's vmid-200 VM, not beta's."""
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[proxmox_nodes]\n"
        "alpha-01 ansible_host=10.0.0.1 cluster=alpha\n"
        "beta-01 ansible_host=10.1.0.1 cluster=beta\n"
        "[proxmox_vms]\n"
        "alpha-vm ansible_host=10.0.1.1 vmid=200 pve_node=alpha-01\n"
        "beta-vm ansible_host=10.1.1.1 vmid=200 pve_node=beta-01\n"
        "[remote_hosts]\n[custom_hosts]\n"
    )
    scanned: List[str] = []

    def _factory(host, **kw):
        scanned.append(host)
        return ScriptedExecutor(script={"which apt-get": [_ok("apt")], "dist-upgrade": [_ok("")]})

    _patch_executors(monkeypatch, _factory)
    monkeypatch.setattr(scan_mod, "_discover_lxcs", lambda ex, s, **kw: [])

    settings = GlobalSettings(fleet_history_enabled=False)
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv), limit={"alpha/200"})
    assert rc == 0
    assert "alpha-vm" in scanned  # qualified token selects alpha's VM
    assert "beta-vm" not in scanned  # beta's same-vmid VM stays out


def test_run_fleet_scan_bare_vm_limit_selects_vmid_in_every_cluster(tmp_path, monkeypatch):
    """A bare vmid limit token keeps today's behaviour: both clusters' VMs scan."""
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[proxmox_nodes]\n"
        "alpha-01 ansible_host=10.0.0.1 cluster=alpha\n"
        "beta-01 ansible_host=10.1.0.1 cluster=beta\n"
        "[proxmox_vms]\n"
        "alpha-vm ansible_host=10.0.1.1 vmid=200 pve_node=alpha-01\n"
        "beta-vm ansible_host=10.1.1.1 vmid=200 pve_node=beta-01\n"
        "[remote_hosts]\n[custom_hosts]\n"
    )
    scanned: List[str] = []

    def _factory(host, **kw):
        scanned.append(host)
        return ScriptedExecutor(script={"which apt-get": [_ok("apt")], "dist-upgrade": [_ok("")]})

    _patch_executors(monkeypatch, _factory)
    monkeypatch.setattr(scan_mod, "_discover_lxcs", lambda ex, s, **kw: [])

    settings = GlobalSettings(fleet_history_enabled=False)
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv), limit={"200"})
    assert rc == 0
    assert "alpha-vm" in scanned
    assert "beta-vm" in scanned


def test_run_fleet_scan_error_sets_exit_code(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text("[remote_hosts]\nweb-01\n[proxmox_nodes]\n[proxmox_vms]\n[custom_hosts]\n")

    def _factory(host, **kw):
        return ScriptedExecutor(script={"which apt-get": [_ok("apt")], "dist-upgrade": [_fail(stderr="down")]})

    _patch_executors(monkeypatch, _factory)
    settings = GlobalSettings(fleet_history_enabled=False)
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv))
    assert rc == 1


_TRUENAS_AVAILABLE = """\
TrueNAS-SCALE-24.10.2.2
@@MANUAL_UPDATE_SEPARATOR@@
{"status":"AVAILABLE","version":"TrueNAS-SCALE-25.04.2.1","train":"25.04-STABLE","changes":[]}
"""


def test_run_fleet_scan_persists_manual_host_and_overrides(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[manual_update_hosts]\n"
        "truenas ansible_host=10.0.0.30 manual_adapter=truenas_scale\n"
        "[remote_hosts]\n[proxmox_nodes]\n[proxmox_vms]\n[custom_hosts]\n",
        encoding="utf-8",
    )
    host_vars = tmp_path / "host_vars"
    host_vars.mkdir()
    (host_vars / "truenas.yml").write_text(
        "display_name: Storage NAS\napply_hint: Open the maintenance UI\n",
        encoding="utf-8",
    )
    constructions = []

    def _factory(host, **kw):
        constructions.append((host, kw))
        return ScriptedExecutor(script={"midclt": [_ok(_TRUENAS_AVAILABLE)]})

    _patch_executors(monkeypatch, _factory)
    settings = GlobalSettings(
        fleet_history_dir=str(tmp_path / "hist"),
        host_vars_dir=str(host_vars),
        manual_update_notifications=False,
    )
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv))

    assert rc == 0
    assert constructions == [("truenas", {"inventory": str(inv), "check": False})]
    latest = json.loads((tmp_path / "hist" / "pending-latest.json").read_text())
    assert latest["manual"]["truenas"] == {
        "host": "truenas",
        "display_name": "Storage NAS",
        "adapter": "truenas_scale",
        "current": "24.10.2.2",
        "latest": "25.04.2.1",
        "update_available": True,
        "reboot_required": False,
        "summary": "TrueNAS update available: 24.10.2.2 -> 25.04.2.1 (train 25.04-STABLE)",
        "details": [],
        "apply_hint": "Open the maintenance UI",
        "unreachable": False,
        "error": None,
    }


def test_run_fleet_scan_manual_api_host_builds_no_executor(tmp_path, monkeypatch):
    """API-transport manual hosts run manager-side HTTPS; no RunnerExecutor."""
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[manual_update_hosts]\n"
        "firewall ansible_host=10.0.0.1 manual_adapter=opnsense_api "
        "api_key=KEY api_secret=SECRET verify_ssl=false\n"
        "[remote_hosts]\n[proxmox_nodes]\n[proxmox_vms]\n[custom_hosts]\n",
        encoding="utf-8",
    )
    constructions = []
    _patch_executors(
        monkeypatch, lambda host, **kw: constructions.append(host) or ScriptedExecutor()
    )
    calls = []

    def _get_json(url, **kw):
        calls.append(("GET", url, kw))
        if "firmware/upgradestatus" in url:
            return {"status": "done"}
        if "firmware/status" in url:
            return {"status": "nothing_to_do"}
        return {"version": "24.1.10"}

    def _post_json(url, payload, **kw):
        calls.append(("POST", url, kw))
        return http_mod.HttpResponse(status=200, body="{}")

    monkeypatch.setattr(http_mod, "get_json", _get_json)
    monkeypatch.setattr(http_mod, "post_json", _post_json)
    settings = GlobalSettings(
        fleet_history_dir=str(tmp_path / "hist"),
        manual_update_notifications=False,
        manual_update_api_timeout=7.0,
    )
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv))

    assert rc == 0
    assert constructions == []  # API transport never builds an SSH executor
    # info, check POST, upgradestatus, status
    assert [c[0] for c in calls] == ["GET", "POST", "GET", "GET"]
    assert calls[1][1].endswith("/api/core/firmware/check")
    for _, url, kw in calls:
        assert url.startswith("https://10.0.0.1/api/core/firmware/")
        assert kw["timeout"] == 7.0  # settings.manual_update_api_timeout
        assert kw["verify"] is False  # per-host verify_ssl=false
        assert kw["headers"]["Authorization"] == "Basic " + base64.b64encode(b"KEY:SECRET").decode()
    latest = json.loads((tmp_path / "hist" / "pending-latest.json").read_text())
    assert latest["manual"]["firewall"] == {
        "host": "firewall",
        "display_name": "OPNsense",
        "adapter": "opnsense_api",
        "current": "24.1.10",
        "latest": "",
        "update_available": False,
        "reboot_required": False,
        "summary": "OPNsense is up to date (24.1.10)",
        "details": ["Installed: 24.1.10", "Firmware check status: nothing_to_do"],
        "apply_hint": "OPNsense GUI → System → Firmware → Status",
        "unreachable": False,
        "error": None,
    }


def test_run_fleet_scan_manual_api_missing_key_aborts_before_executor(tmp_path, monkeypatch):
    """Missing api_key on an *_api host fails loudly before any host contact."""
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[manual_update_hosts]\n"
        "nas manual_adapter=truenas_scale_api\n"
        "[remote_hosts]\n[proxmox_nodes]\n[proxmox_vms]\n[custom_hosts]\n",
        encoding="utf-8",
    )
    contacted = []
    _patch_executors(monkeypatch, lambda host, **kw: contacted.append(host))
    with pytest.raises(SystemExit, match="api_key is required"):
        scan_mod.run_fleet_scan(
            settings=GlobalSettings(fleet_history_enabled=False),
            inventory_path=str(inv),
        )
    assert contacted == []


def test_run_fleet_scan_manual_limit_selects_name(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[manual_update_hosts]\n"
        "nas-a manual_adapter=truenas_scale\n"
        "nas-b manual_adapter=truenas_scale\n"
        "[remote_hosts]\n[proxmox_nodes]\n[proxmox_vms]\n[custom_hosts]\n",
        encoding="utf-8",
    )
    contacted = []

    def _factory(host, **kw):
        contacted.append(host)
        return ScriptedExecutor(script={"midclt": [_ok(_TRUENAS_AVAILABLE)]})

    _patch_executors(monkeypatch, _factory)
    settings = GlobalSettings(fleet_history_enabled=False, manual_update_notifications=False)
    assert scan_mod.run_fleet_scan(
        settings=settings, inventory_path=str(inv), limit={"nas-b"}
    ) == 0
    assert contacted == ["nas-b"]


def test_run_fleet_scan_manual_unreachable_does_not_fail(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[manual_update_hosts]\nfirewall manual_adapter=opnsense\n"
        "[remote_hosts]\n[proxmox_nodes]\n[proxmox_vms]\n[custom_hosts]\n",
        encoding="utf-8",
    )

    def _factory(host, **kw):
        return ScriptedExecutor(
            default=PrimitiveResult(
                rc=4, failed=True, unreachable=True, stderr=_UNREACHABLE_ERR
            )
        )

    _patch_executors(monkeypatch, _factory)
    settings = GlobalSettings(
        fleet_history_dir=str(tmp_path / "hist"), manual_update_notifications=False
    )
    assert scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv)) == 0
    entry = json.loads((tmp_path / "hist" / "pending-latest.json").read_text())["manual"]["firewall"]
    assert entry["unreachable"] is True
    assert entry["error"]


def test_run_fleet_scan_manual_parser_error_fails(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[manual_update_hosts]\nnas manual_adapter=truenas_scale\n"
        "[remote_hosts]\n[proxmox_nodes]\n[proxmox_vms]\n[custom_hosts]\n",
        encoding="utf-8",
    )
    malformed = "TrueNAS-SCALE-24.10\n@@MANUAL_UPDATE_SEPARATOR@@\n{bad json"
    _patch_executors(
        monkeypatch,
        lambda host, **kw: ScriptedExecutor(script={"midclt": [_ok(malformed)]}),
    )
    settings = GlobalSettings(
        fleet_history_dir=str(tmp_path / "hist"), manual_update_notifications=False
    )
    assert scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv)) == 1
    entry = json.loads((tmp_path / "hist" / "pending-latest.json").read_text())["manual"]["nas"]
    assert "Malformed" in entry["error"]
    assert entry["unreachable"] is False


def test_run_fleet_scan_validates_manual_safety_before_executor(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[manual_update_hosts]\nnas manual_adapter=truenas_scale\n"
        "[remote_hosts]\nnas\n[proxmox_nodes]\n[proxmox_vms]\n[custom_hosts]\n",
        encoding="utf-8",
    )
    contacted = []
    _patch_executors(monkeypatch, lambda host, **kw: contacted.append(host))
    with pytest.raises(SystemExit, match="manual-update overlap"):
        scan_mod.run_fleet_scan(
            settings=GlobalSettings(fleet_history_enabled=False),
            inventory_path=str(inv),
        )
    assert contacted == []


def test_run_fleet_scan_rejects_unknown_adapter_before_executor(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[manual_update_hosts]\nappliance manual_adapter=unknown\n"
        "[remote_hosts]\n[proxmox_nodes]\n[proxmox_vms]\n[custom_hosts]\n",
        encoding="utf-8",
    )
    contacted = []
    _patch_executors(monkeypatch, lambda host, **kw: contacted.append(host))
    with pytest.raises(SystemExit, match="Unknown manual update adapter"):
        scan_mod.run_fleet_scan(
            settings=GlobalSettings(fleet_history_enabled=False),
            inventory_path=str(inv),
        )
    assert contacted == []


def test_run_fleet_scan_manual_notification_reuses_all_notifiers(tmp_path, monkeypatch):
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[manual_update_hosts]\nnas manual_adapter=truenas_scale\n"
        "[remote_hosts]\n[proxmox_nodes]\n[proxmox_vms]\n[custom_hosts]\n",
        encoding="utf-8",
    )
    _patch_executors(
        monkeypatch,
        lambda host, **kw: ScriptedExecutor(
            script={"midclt": [_ok(_TRUENAS_AVAILABLE)]}
        ),
    )
    destinations = [
        {"type": "discord", "webhook": "https://discord.test"},
        {"type": "ntfy", "url": "https://ntfy.test"},
        {"type": "webhook", "url": "https://webhook.test"},
        {"type": "telegram", "bot_token": "x", "chat_id": "1"},
    ]
    calls = []
    monkeypatch.setattr(
        scan_mod.notifiers,
        "dispatch",
        lambda resolved, **kw: calls.append((resolved, kw)),
    )
    settings = GlobalSettings(
        fleet_history_dir=str(tmp_path / "hist"),
        fleet_history_enabled=False,
        notifiers=destinations,
    )
    assert scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv)) == 0
    assert len(calls) == 1
    assert calls[0][0] == destinations
    assert calls[0][1]["failed"] is False
    assert "Manual updates required" in calls[0][1]["body"]

    # The unchanged state is below the 24-hour reminder boundary.
    assert scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv)) == 0
    assert len(calls) == 1


# --- pending_summary / read_pending (readers) ------------------------------------


def _write_scan(tmp_path, *, ts, hosts=None, lxc=None, manual=None):
    scan_mod.write_pending(
        {
            "timestamp": ts,
            "hosts": hosts or {},
            "lxc": lxc or {},
            "manual": manual or {},
        },
        history_dir=tmp_path,
        keep=0,
    )


def test_pending_summary_newest_first_with_aggregates(tmp_path):
    _write_scan(tmp_path, ts="20260101T000000000000Z")
    _write_scan(
        tmp_path,
        ts="20260102T000000000000Z",
        hosts={
            "web-01": {
                "kind": "remote",
                "pkg_mgr": "apt",
                "pending_count": 3,
                "pending": ["a", "b", "c"],
                "error": None,
            },
            "pve-01": {"kind": "node", "pkg_mgr": "apt", "pending_count": 1, "pending": ["d"], "error": "ssh down"},
        },
        lxc={
            "101": {
                "node": "pve-01",
                "name": "sonarr",
                "skipped": None,
                "os_pending_count": 2,
                "os_pending": ["x", "y"],
                "app": {"script": "sonarr", "current": "1", "latest": "2", "outdated": True},
                "error": None,
            }
        },
    )
    rows = scan_mod.pending_summary(tmp_path)
    assert [r["timestamp"] for r in rows] == ["20260102T000000000000Z", "20260101T000000000000Z"]
    newest = rows[0]
    assert newest["hosts_pending"] == 4
    assert newest["lxc_os_pending"] == 2
    assert newest["outdated_apps"] == 1
    assert newest["errors"] == 1


def test_pending_summary_counts_manual_actions_and_failures(tmp_path):
    _write_scan(
        tmp_path,
        ts="20260103T000000000000Z",
        manual={
            "nas": {
                "update_available": True,
                "reboot_required": False,
                "unreachable": False,
                "error": None,
            },
            "firewall": {
                "update_available": False,
                "reboot_required": True,
                "unreachable": False,
                "error": None,
            },
            "broken": {
                "update_available": False,
                "reboot_required": False,
                "unreachable": False,
                "error": "unknown firmware output",
            },
            "offline": {
                "update_available": False,
                "reboot_required": False,
                "unreachable": True,
                "error": "ssh timeout",
            },
        },
    )
    row = scan_mod.pending_summary(tmp_path)[0]
    assert row["manual_updates"] == 1
    assert row["manual_reboots"] == 1
    assert row["errors"] == 1
    assert row["unreachable"] == 1


def test_pending_summary_legacy_snapshot_has_zero_manual_counts(tmp_path):
    # Deliberately omit the manual mapping: old persisted snapshots must load.
    scan_mod.write_pending(
        {"timestamp": "20260101T000000000000Z", "hosts": {}, "lxc": {}},
        history_dir=tmp_path,
        keep=0,
    )
    row = scan_mod.pending_summary(tmp_path)[0]
    assert row["manual_updates"] == 0
    assert row["manual_reboots"] == 0


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

_DF_90 = (
    "Filesystem     1024-blocks    Used Available Capacity Mounted on\n"
    "/dev/rbd17         4046560 3416796    403668      90% /\n"
)
_OSREL_BOOKWORM = 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nVERSION_ID="12"\nID=debian\n'
_OSREL_TRIXIE = 'VERSION_ID="13"\nID=debian\n'


def _patch_github_trixie(monkeypatch, *, latest="v4.1.0"):
    """ct script that both names a GH repo and targets debian 13."""
    ct_script = (
        'var_os="${var_os:-debian}"\nvar_version="${var_version:-13}"\ncheck_for_gh_release "sonarr" "Sonarr/Sonarr"\n'
    )
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: type("R", (), {"body": ct_script})())
    monkeypatch.setattr(http_mod, "get_json", lambda url, **kw: {"tag_name": latest})


def test_scan_lxc_reports_disk_and_os(monkeypatch):
    _patch_github_trixie(monkeypatch)
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok(APT_SIM)], "cat ~/.sonarr": [_ok("4.0.17")]},
        introspect_facts=dict(_INTROSPECT_RUNNING, df_stdout=_DF_90, os_release_stdout=_OSREL_TRIXIE),
    )
    result = scan_mod.scan_lxc(ex, "130", "pve-01")
    assert result["disk_percent"] == 90
    assert result["os"] == "debian 13"
    assert result["os_mismatch"] is None


def test_scan_lxc_flags_os_mismatch(monkeypatch):
    _patch_github_trixie(monkeypatch)
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok("")], "cat ~/.sonarr": [_ok("4.0.17")]},
        introspect_facts=dict(_INTROSPECT_RUNNING, df_stdout=_DF_90, os_release_stdout=_OSREL_BOOKWORM),
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
            "123": {
                "os_pending_count": 0,
                "app": None,
                "disk_percent": 63,
                "os_mismatch": "container runs debian 12 but ...",
                "error": None,
            },
            "130": {"os_pending_count": 4, "app": None, "disk_percent": 90, "os_mismatch": None, "error": None},
            "105": {"os_pending_count": 0, "app": None, "disk_percent": 52, "os_mismatch": None, "error": None},
        },
    }
    (tmp_path / "pending-2026-07-23T04-00-00Z.json").write_text(json.dumps(scan))
    rows = scan_mod.pending_summary(tmp_path, disk_threshold=75)
    assert rows[0]["low_disk"] == 1  # only 130 at 90%
    assert rows[0]["os_mismatch"] == 1  # only 123


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


def test_pending_summary_counts_security_and_reboot(tmp_path):
    scan = {
        "timestamp": "2026-07-23T04-00-00Z",
        "hosts": {
            "web-01": {"pending_count": 2, "security_count": 1, "reboot_required": True, "error": None},
            "pve-01": {"pending_count": 0, "security_count": 0, "reboot_required": False, "error": None},
        },
        "lxc": {
            "101": {"os_pending_count": 1, "os_security_count": 2, "reboot_required": True, "error": None},
            "102": {"os_pending_count": 0, "os_security_count": 0, "reboot_required": False, "error": None},
        },
    }
    (tmp_path / "pending-2026-07-23T04-00-00Z.json").write_text(json.dumps(scan))
    rows = scan_mod.pending_summary(tmp_path)
    assert rows[0]["security_pending"] == 3  # 1 host + 2 containers
    assert rows[0]["reboot_hosts"] == 2  # web-01 + lxc 101


def test_pending_summary_tolerates_scans_without_security_keys(tmp_path):
    """Pre-PR2 snapshots lack security/reboot keys — aggregates read 0."""
    scan = {
        "timestamp": "2026-07-01T04-00-00Z",
        "hosts": {"web-01": {"pending_count": 1, "error": None}},
        "lxc": {"101": {"os_pending_count": 2, "error": None}},
    }
    (tmp_path / "pending-2026-07-01T04-00-00Z.json").write_text(json.dumps(scan))
    rows = scan_mod.pending_summary(tmp_path)
    assert rows[0]["security_pending"] == 0
    assert rows[0]["reboot_hosts"] == 0


def test_scan_lxc_returns_both_the_cluster_id_and_the_health_signals(monkeypatch):
    """scan_lxc's result dict is the union of #38's `id` and #41's health keys."""
    _patch_github_trixie(monkeypatch)
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok(APT_SIM)], "cat ~/.sonarr": [_ok("4.0.17")]},
        introspect_facts=dict(_INTROSPECT_RUNNING, df_stdout=_DF_90, os_release_stdout=_OSREL_BOOKWORM),
    )
    result = scan_mod.scan_lxc(ex, "123", "Hammond")
    assert result["id"] == "123"
    assert result["node"] == "Hammond"
    assert result["disk_percent"] == 90
    assert result["os"] == "debian 12"
    assert result["os_mismatch"] is not None


# --- unreachable hosts must not fail a read-only scan ---------------------------

_UNREACHABLE_ERR = (
    "Data could not be sent to remote host 10.10.10.40. "
    "Make sure this host can be reached over ssh: "
    "ssh: connect to host 10.10.10.40 port 22: No route to host"
)


def test_scan_host_flags_unreachable_rather_than_a_plain_error():
    class Dead:
        host = "ONeill"

        def run_shell(self, command, **opts):
            return PrimitiveResult(rc=4, failed=True, unreachable=True, stderr=_UNREACHABLE_ERR)

    result = scan_mod.scan_host(Dead())
    assert result["unreachable"] is True
    assert result["error"]


def test_scan_host_genuine_failure_is_not_flagged_unreachable():
    ex = ScriptedExecutor(default=_ok("bash: apt-get: command not found", rc=127))
    ex.default = PrimitiveResult(rc=127, failed=True, stderr="boom: disk I/O error")
    result = scan_mod.scan_host(ex)
    assert result.get("unreachable") is False
    assert result["error"]


def test_scan_lxc_typed_unreachable_is_flagged():
    class Dead:
        host = "ONeill"

        def introspect(self, lxc_id):
            raise UnreachableHostError("node unreachable: No route to host")

    result = scan_mod.scan_lxc(Dead(), "101", "ONeill")
    assert result["unreachable"] is True
    assert result["error"]


def test_empty_lxc_entry_has_the_same_shape_as_a_real_scan(monkeypatch):
    """The drift guard: the fallback dict must not miss keys scan_lxc returns."""
    _patch_github_trixie(monkeypatch)
    ex = ScriptedExecutor(
        script={"dist-upgrade": [_ok(APT_SIM)], "cat ~/.sonarr": [_ok("4.0.17")]},
        introspect_facts=dict(_INTROSPECT_RUNNING, df_stdout=_DF_90, os_release_stdout=_OSREL_TRIXIE),
    )
    real = scan_mod.scan_lxc(ex, "101", "pve-01")
    stub = scan_mod._empty_lxc_entry("pve-01", "101")
    assert set(stub) == set(real)


def test_pending_summary_separates_unreachable_from_errors(tmp_path):
    scan = {
        "timestamp": "2026-07-23T10-00-00Z",
        "hosts": {
            "ONeill": {"pending_count": 0, "unreachable": True, "error": _UNREACHABLE_ERR},
            "web-01": {"pending_count": 0, "unreachable": False, "error": "scan command failed"},
        },
        "lxc": {},
    }
    (tmp_path / "pending-2026-07-23T10-00-00Z.json").write_text(json.dumps(scan))
    row = scan_mod.pending_summary(tmp_path)[0]
    assert row["unreachable"] == 1  # ONeill
    assert row["errors"] == 1  # web-01 only


def test_pending_summary_tolerates_scans_without_the_unreachable_key(tmp_path):
    scan = {"timestamp": "2026-07-01T04-00-00Z", "hosts": {"web-01": {"pending_count": 0, "error": "boom"}}, "lxc": {}}
    (tmp_path / "pending-2026-07-01T04-00-00Z.json").write_text(json.dumps(scan))
    row = scan_mod.pending_summary(tmp_path)[0]
    assert row["unreachable"] == 0
    assert row["errors"] == 1


def test_run_fleet_scan_unreachable_node_does_not_set_exit_code(tmp_path, monkeypatch):
    """The fleet-scan.service failure: ONeill down made a read-only scan exit 1.

    A reachable node is still scanned, and the run reports success.
    """
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[proxmox_nodes]\nONeill ansible_host=10.10.10.40\npve-01 ansible_host=10.0.0.1\n"
        "[remote_hosts]\n[proxmox_vms]\n[custom_hosts]\n"
    )
    _patch_github(monkeypatch)

    def _factory(host, **kw):
        if host == "ONeill":
            ex = ScriptedExecutor(default=PrimitiveResult(rc=4, failed=True, unreachable=True, stderr=_UNREACHABLE_ERR))
        else:
            ex = ScriptedExecutor(
                script={
                    "which apt-get": [_ok("apt")],
                    "dist-upgrade": [_ok(APT_SIM), _ok(APT_SIM)],
                    "cat ~/.sonarr": [_ok("4.0.17")],
                },
                introspect_facts=_INTROSPECT_RUNNING,
            )
        ex.host = host  # class default is "web-01"; the stub below keys off it
        return ex

    _patch_executors(monkeypatch, _factory)

    def _discover(ex, s, **kw):
        if getattr(ex, "host", "") == "ONeill":
            raise UnreachableHostError("node unreachable: No route to host")
        return ["101"]

    monkeypatch.setattr(scan_mod, "_discover_lxcs", _discover)

    settings = GlobalSettings(fleet_history_dir=str(tmp_path / "hist"))
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv))

    assert rc == 0, "an unreachable node must not fail a read-only scan"
    latest = json.loads((tmp_path / "hist" / "pending-latest.json").read_text())
    assert latest["hosts"]["ONeill"]["unreachable"] is True
    assert latest["lxc"]["ONeill"]["skipped"] == "unreachable"
    # the surviving node was still scanned
    assert latest["lxc"]["pve-01/101"]["name"] == "sonarr"


def test_run_fleet_scan_genuine_error_still_sets_exit_code(tmp_path, monkeypatch):
    """Tolerating unreachable must not swallow a real failure on a live node."""
    inv = tmp_path / "hosts.ini"
    inv.write_text("[proxmox_nodes]\npve-01 ansible_host=10.0.0.1\n[remote_hosts]\n[proxmox_vms]\n[custom_hosts]\n")

    def _factory(host, **kw):
        return ScriptedExecutor(
            script={"which apt-get": [_ok("apt")], "dist-upgrade": [_fail(stderr="disk I/O error")]}
        )

    _patch_executors(monkeypatch, _factory)
    monkeypatch.setattr(scan_mod, "_discover_lxcs", lambda ex, s: [])

    settings = GlobalSettings(fleet_history_enabled=False)
    rc = scan_mod.run_fleet_scan(settings=settings, inventory_path=str(inv))
    assert rc == 1


# --- PR3: per-host ledger wiring -------------------------------------------- #


def _scan_fixture(ts, version):
    return {
        "timestamp": ts,
        "hosts": {
            "web-01": {
                "kind": "remote",
                "os_release": {"id": "debian", "version_id": version, "pretty_name": f"Debian {version}"},
            }
        },
        "lxc": {
            "pve-01/101": {
                "node": "pve-01",
                "id": "101",
                "name": "sonarr",
                "os_release": {"id": "debian", "version_id": version, "pretty_name": f"Debian {version}"},
            }
        },
        "pkg_mgr": "apt",
        "pending_count": 0,
        "pending": [],
    }


def test_write_pending_observes_ledger(tmp_path):
    """write_pending feeds hosts.json: baseline first, then a version change
    emits an os-upgrade event under the multi-cluster-safe keys."""
    scan_mod.write_pending(_scan_fixture("20260101T000000000000Z", "11"), history_dir=tmp_path, keep=0)
    data = json.loads((tmp_path / "hosts.json").read_text())
    assert data["events"] == []  # first observation: baseline
    assert data["hosts"]["web-01"]["os_release"]["version_id"] == "11"
    assert data["hosts"]["pve-01/101"]["os_release"]["version_id"] == "11"

    scan_mod.write_pending(_scan_fixture("20260102T000000000000Z", "12"), history_dir=tmp_path, keep=0)
    data = json.loads((tmp_path / "hosts.json").read_text())
    assert data["events"] == [
        {"type": "os-upgrade", "host": "pve-01/101", "from": "11", "to": "12", "ts": "20260102T000000000000Z"},
        {"type": "os-upgrade", "host": "web-01", "from": "11", "to": "12", "ts": "20260102T000000000000Z"},
    ]


def test_write_pending_corrupt_ledger_does_not_fail(tmp_path):
    """A corrupt hosts.json must never fail a scan — write_pending recovers."""
    (tmp_path / "hosts.json").write_text("not json", encoding="utf-8")
    scan_mod.write_pending(_scan_fixture("20260101T000000000000Z", "11"), history_dir=tmp_path, keep=0)
    data = json.loads((tmp_path / "hosts.json").read_text())
    assert data["hosts"]["web-01"]["os_release"]["version_id"] == "11"
