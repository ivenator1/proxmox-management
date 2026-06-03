"""Tests for proxmox_fleet.flows.lxc — the lxc_update control flow.

Uses a ScriptedLxcExecutor (no real Ansible) and monkeypatches http for
GitHub/Kuma calls. Asserts control flow: rescue/rollback, snapshot gating,
health check, dry-run, was_stopped handling, resource scaling, and status strings.
"""
import time

import pytest

from proxmox_fleet import http as http_mod
from proxmox_fleet.flows import lxc as flow_mod
from proxmox_fleet.flows.lxc import LxcFlowOutcome, run_lxc_update
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(stdout="", changed=True, rc=0):
    return PrimitiveResult(rc=rc, changed=changed, stdout=stdout, failed=False)


def _fail(rc=1, stderr="boom"):
    return PrimitiveResult(rc=rc, failed=True, stderr=stderr)


PCT_CONFIG_RUNNING = "hostname: sonarr\nostype: debian\n"
PCT_CONFIG_TEMPLATE = "hostname: sonarr\nostype: debian\ntemplate: 1\n"
PCT_CONFIG_ALPINE = "hostname: sonarr\nostype: alpine\n"
PCT_STATUS_RUNNING = "status: running"
PCT_STATUS_STOPPED = "status: stopped"


class ScriptedLxcExecutor:
    """Fake executor for lxc flow tests.

    run_shell() responses are keyed by command substring; first match wins.
    snapshot() scripting is separate.
    """

    host = "pve-01"

    def __init__(self, script=None, default=None, snap_changed=True):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default if default is not None else _ok()
        self.commands = []
        self.reboots = 0
        self.snap_changed = snap_changed
        self.snapshots_created = []
        self.snapshots_deleted = []
        self.rollback_called = False

    def _resp(self, command):
        for key, queue in self.script.items():
            if key in command and queue:
                return queue.pop(0)
        return self.default

    def run_shell(self, command, **opts):
        self.commands.append(command)
        if "pct rollback" in command:
            self.rollback_called = True
        return self._resp(command)

    def run_local(self, command):
        return self._resp(command)

    def reboot(self, *, timeout=600):
        self.reboots += 1
        return _ok()

    def snapshot(self, lxc_id, *, snap_state, **api_params):
        if snap_state == "present":
            self.snapshots_created.append(lxc_id)
        else:
            self.snapshots_deleted.append(lxc_id)
        return _ok(changed=self.snap_changed)


def _settings(**overrides):
    """Build a minimal GlobalSettings for tests."""
    base = {
        "lxc_backup_strategy": "snapshot",
        "lxc_backup_storage": "local",
        "lxc_auto_reboot": False,
        "lxc_unattended": True,
        "pve_api_user": "root@pam",
        "pve_api_token_id": "tok",
        "pve_api_token_secret": "secret",
    }
    base.update(overrides)
    return GlobalSettings.model_validate(base)


def _exec_normal(ver_before="1.0", ver_after="1.1", os_stdout="2 upgraded, 0 newly installed"):
    """Executor that simulates a happy-path update with version change."""
    return ScriptedLxcExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_RUNNING)],
        "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
        "pct pull": [_ok(rc=0, stdout="", changed=False)],
        "grep": [_ok(stdout="sonarr")],
        "rm -f": [_ok(changed=False)],
        # version reads (before and after)
        "cat ~/.sonarr": [_ok(stdout=ver_before), _ok(stdout=ver_after)],
        # OS update
        "apt-get": [_ok(stdout=os_stdout, changed=True)],
        # dpkg hash before and after
        "dpkg-query": [_ok(stdout="hash_before  -\n", changed=False),
                       _ok(stdout="hash_before  -\n", changed=False)],
        # reboot check — no reboot needed
        "test -f /var/run/reboot-required": [_fail(rc=1)],
        # pct stop/start not needed
    })


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_version_updated(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = _exec_normal(ver_before="1.0", ver_after="1.1")
    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert out.failed is False
    assert out.changed is True
    assert out.record is not None
    assert out.record.app == "Updated: 1.0 → 1.1"
    assert out.record.node == "pve-01"
    assert out.record.id == "101"


def test_version_unchanged_noop(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = _exec_normal(ver_before="1.0", ver_after="1.0", os_stdout="0 upgraded, 0 newly installed")
    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert out.record is None  # idle OK is suppressed
    assert out.changed is False
    assert out.failed is False


CT_SCRIPT_WITH_REPO = (
    '#!/usr/bin/env bash\nAPP="sonarr"\n'
    'check_for_gh_release "Sonarr/Sonarr" "Sonarr/Sonarr"\n'
    'var_cpu="2"\npct set $CTID -cores 2\n'
)


def test_dry_run(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    # ct script fetch returns a script with gh_repo; GitHub releases returns v1.5
    monkeypatch.setattr(
        http_mod, "request",
        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_WITH_REPO),
    )
    monkeypatch.setattr(http_mod, "get_json", lambda url, **kw: {"tag_name": "v1.5"})
    ex = ScriptedLxcExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_RUNNING)],
        "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
        "pct pull": [_ok(rc=0, changed=False)],
        "grep": [_ok(stdout="sonarr")],
        "rm -f": [_ok(changed=False)],
        "cat ~/.sonarr": [_ok(stdout="1.4")],
    })
    out = run_lxc_update("pve-01", "101", ex, _settings(), dry_run=True, api_host="192.168.1.10")
    assert out.record is not None
    # gh_repo resolves → installed vs tag → "1.4 → v1.5"
    assert "→" in out.record.app
    assert out.changed is False
    # Ensure no mutations were called (no apt-get, no snapshot)
    assert not any("apt-get" in c for c in ex.commands)
    assert ex.snapshots_created == []


def test_no_update_script(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    # pct pull fails → lxc_no_update_script = True
    ex = ScriptedLxcExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_RUNNING)],
        "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
        "pct pull": [_fail(rc=1)],
        # OS update
        "apt-get": [_ok(stdout="0 upgraded, 0 newly installed")],
        # dpkg hash (not collected for no_update_script, but OS hash might still run)
        "reboot-required": [_fail(rc=1)],
    })
    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert out.record is not None
    assert out.record.app == "NO SCRIPT"
    assert out.failed is False


def test_template_skipped(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ex = ScriptedLxcExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_TEMPLATE)],
    })
    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert out.record is None
    assert out.failed is False
    # Only pct config should have been called
    assert len(ex.commands) == 1
    assert "pct config" in ex.commands[0]


# ---------------------------------------------------------------------------
# Snapshot / rollback
# ---------------------------------------------------------------------------


def test_snapshot_rollback_on_failure(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))

    # OS update raises (triggers rescue after snapshot taken)
    class FailingExecutor(ScriptedLxcExecutor):
        def run_shell(self, command, **opts):
            self.commands.append(command)
            if "apt-get" in command:
                raise RuntimeError("apt-get failed")
            if "pct rollback" in command:
                self.rollback_called = True
                return _ok()
            if "pct status" in command and self.rollback_called:
                return _ok(stdout=PCT_STATUS_RUNNING)
            return self._resp(command)

    ex = FailingExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_RUNNING)],
        "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
        "pct pull": [_ok(rc=0, changed=False)],
        "grep": [_ok(stdout="sonarr")],
        "rm -f": [_ok(changed=False)],
        "cat ~/.sonarr": [_ok(stdout="1.0")],
        "dpkg-query": [_ok(stdout="hash  -\n", changed=False)],
    }, snap_changed=True)

    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert out.failed is True
    assert out.record.app == "FAILED + ROLLED BACK"
    assert ex.rollback_called is True
    assert ex.snapshots_created == ["101"]
    assert ex.snapshots_deleted == ["101"]  # always block cleans up


def test_no_rollback_without_snapshot(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))

    class FailingExecutor(ScriptedLxcExecutor):
        def run_shell(self, command, **opts):
            self.commands.append(command)
            if "apt-get" in command:
                raise RuntimeError("apt-get failed")
            return self._resp(command)

    ex = FailingExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_RUNNING)],
        "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
        "pct pull": [_ok(rc=0, changed=False)],
        "grep": [_ok(stdout="sonarr")],
        "rm -f": [_ok(changed=False)],
        "cat ~/.sonarr": [_ok(stdout="1.0")],
        "dpkg-query": [_ok(stdout="hash  -\n", changed=False)],
    })
    settings = _settings(lxc_backup_strategy="none")

    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert out.failed is True
    # strategy=none → no snapshot attempted → snapshot_failed=False → plain "FAILED"
    assert out.record.app == "FAILED"
    assert ex.rollback_called is False
    assert ex.snapshots_created == []


def test_snapshot_failure_warning(monkeypatch):
    """snapshot() returns changed=False → warning appended, update continues."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = ScriptedLxcExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_RUNNING)],
        "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
        "pct pull": [_fail(rc=1)],  # no update script
        "apt-get": [_ok(stdout="0 upgraded, 0 newly installed")],
        "reboot-required": [_fail(rc=1)],
    }, snap_changed=False)  # snapshot API returns changed=False

    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert len(out.warnings) >= 1
    assert any("snapshot" in w.warning.lower() for w in out.warnings)
    assert out.failed is False


def test_vzdump_failure_triggers_rescue(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))

    class VzdumpFailExecutor(ScriptedLxcExecutor):
        def run_shell(self, command, **opts):
            self.commands.append(command)
            if "vzdump" in command:
                raise RuntimeError("vzdump failed")
            return self._resp(command)

    ex = VzdumpFailExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_RUNNING)],
        "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
        "pct pull": [_ok(rc=0, changed=False)],
        "grep": [_ok(stdout="sonarr")],
        "rm -f": [_ok(changed=False)],
    })
    settings = _settings(lxc_backup_strategy="vzdump")

    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert out.failed is True
    # vzdump backup runs BEFORE snapshot; no snapshot was attempted (snap_taken=False,
    # snapshot_failed=False) → plain "FAILED". "FAILED (NO SNAPSHOT)" only fires when
    # a snapshot was explicitly requested but the API returned changed=False.
    assert out.record.app == "FAILED"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_health_check_failure_triggers_rescue(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    monkeypatch.setattr(
        http_mod, "get_json",
        lambda url, **kw: {"heartbeatList": {"5": [{"status": 0}]}},
    )
    ex = _exec_normal(ver_before="1.0", ver_after="1.1")
    settings = _settings(
        lxc_kuma_map={"101": 5},
        kuma_url="http://kuma",
        kuma_slug="fleet",
        kuma_health_check_retries=1,
        kuma_health_check_delay=0,
    )
    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert out.failed is True
    assert "FAILED" in out.record.app


# ---------------------------------------------------------------------------
# was_stopped — container starts then stops in finally
# ---------------------------------------------------------------------------


def test_was_stopped_start_and_stop(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = ScriptedLxcExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_RUNNING)],
        "pct status": [
            _ok(stdout=PCT_STATUS_STOPPED),  # initial status
            _ok(stdout=PCT_STATUS_RUNNING),   # after pct start
        ],
        "pct start": [_ok(changed=True)],
        "pct pull": [_fail(rc=1)],  # no update script
        "apt-get": [_ok(stdout="0 upgraded, 0 newly installed")],
        "reboot-required": [_fail(rc=1)],
        "pct stop": [_ok(changed=True)],
    })
    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert any("pct start" in c for c in ex.commands)
    assert any("pct stop" in c for c in ex.commands)


# ---------------------------------------------------------------------------
# OS update excluded
# ---------------------------------------------------------------------------


def test_os_excluded(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = ScriptedLxcExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_RUNNING)],
        "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
        "pct pull": [_fail(rc=1)],  # no update script
        "reboot-required": [_fail(rc=1)],
    })
    settings = _settings(os_update_exclude_list=["101"])
    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert not any("apt-get" in c for c in ex.commands)
    # OS should be SKIPPED
    if out.record:
        assert out.record.os == "SKIPPED"


# ---------------------------------------------------------------------------
# dpkg hash detects change when versions match
# ---------------------------------------------------------------------------


def test_dpkg_hash_detects_change(monkeypatch):
    """dpkg hash kicks in when there is no version file (empty cat output).

    Version file has priority: if both before/after are non-empty, version wins.
    dpkg hash is the fallback for containers without a version file.
    """
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = ScriptedLxcExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_RUNNING)],
        "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
        "pct pull": [_ok(rc=0, changed=False)],
        "grep": [_ok(stdout="sonarr")],
        "rm -f": [_ok(changed=False)],
        # No version file → empty reads (version fallback to dpkg hash)
        "cat ~/.sonarr": [_ok(stdout=""), _ok(stdout="")],
        "apt-get": [_ok(stdout="2 upgraded, 0 newly installed", changed=True)],
        # dpkg hash DIFFERS → UPDATED
        "dpkg-query": [
            _ok(stdout="hash_before  -\n", changed=False),
            _ok(stdout="hash_after   -\n", changed=False),
        ],
        "reboot-required": [_fail(rc=1)],
    })
    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert out.record is not None
    assert out.record.app == "UPDATED"


# ---------------------------------------------------------------------------
# A1 — Alpine containers read versions with ash, not bash
# ---------------------------------------------------------------------------


def test_alpine_version_read_uses_ash(monkeypatch):
    """Regression: ~/.scriptname must be read with `ash` on Alpine (no bash)."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = ScriptedLxcExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_ALPINE)],
        "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
        "pct pull": [_ok(rc=0, changed=False)],
        "grep": [_ok(stdout="sonarr")],
        "rm -f": [_ok(changed=False)],
        "cat ~/.sonarr": [_ok(stdout="1.0"), _ok(stdout="1.1")],
        # alpine OS update uses apk
        "apk": [_ok(stdout="OK: upgraded", changed=True)],
        "reboot-required": [_fail(rc=1)],
    })
    out = run_lxc_update("pve-01", "101", ex, _settings(lxc_backup_strategy="none"),
                         api_host="192.168.1.10")
    version_reads = [c for c in ex.commands if "cat ~/.sonarr" in c]
    assert version_reads, "expected at least one version read"
    assert all("-- ash -c" in c for c in version_reads), \
        f"Alpine version reads must use ash, got: {version_reads}"
    assert all("-- bash -c" not in c for c in version_reads)
    assert out.record is not None


# ---------------------------------------------------------------------------
# A2 — OS update / dpkg-hash commands pin LC_ALL=C (locale-independent parsing)
# ---------------------------------------------------------------------------


def test_os_and_dpkg_commands_pin_locale(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = _exec_normal(ver_before="1.0", ver_after="1.1")
    run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    apt_cmds = [c for c in ex.commands if "apt-get" in c]
    dpkg_cmds = [c for c in ex.commands if "dpkg-query" in c]
    assert apt_cmds and all("LC_ALL=C" in c for c in apt_cmds)
    assert dpkg_cmds and all("LC_ALL=C" in c for c in dpkg_cmds)


# ---------------------------------------------------------------------------
# Reboot suffix in OS status
# ---------------------------------------------------------------------------


def test_reboot_suffix_in_os_status(monkeypatch):
    """Reboot check runs only when an update script exists (not lxc_no_update_script).

    Use pct pull succeeding + a ct script to allow the reboot check to run.
    """
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = ScriptedLxcExecutor(script={
        "pct config": [_ok(stdout=PCT_CONFIG_RUNNING)],
        "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
        "pct pull": [_ok(rc=0, changed=False)],
        "grep": [_ok(stdout="sonarr")],
        "rm -f": [_ok(changed=False)],
        # version reads
        "cat ~/.sonarr": [_ok(stdout="1.0"), _ok(stdout="1.1")],
        "apt-get": [_ok(stdout="2 upgraded, 0 newly installed", changed=True)],
        "dpkg-query": [
            _ok(stdout="hash  -\n", changed=False),
            _ok(stdout="hash  -\n", changed=False),
        ],
        # reboot-required file present → rc=0
        "reboot-required": [_ok(rc=0, changed=False)],
        "pct reboot": [_ok(changed=True)],
    })
    settings = _settings(lxc_auto_reboot=True, lxc_backup_strategy="none")
    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert out.record is not None
    assert "Rebooted" in out.record.os
