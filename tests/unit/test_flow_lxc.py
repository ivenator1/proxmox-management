"""Tests for proxmox_fleet.flows.lxc — the lxc_update control flow.

Uses a ScriptedLxcExecutor (no real Ansible) and monkeypatches http for
GitHub/Kuma calls. Asserts control flow: rescue/rollback, snapshot gating,
health check, dry-run, was_stopped handling, resource scaling, and status strings.
"""
import time


from proxmox_fleet import http as http_mod
from proxmox_fleet.flows.lxc import _discover_lxcs, run_lxc_update
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

_INTROSPECT_WITH_SCRIPT = {
    "config_stdout": PCT_CONFIG_RUNNING,
    "status_stdout": PCT_STATUS_RUNNING,
    "pull_rc": 0,
    "script_stdout": "source ct/sonarr.sh",
}
_INTROSPECT_NO_SCRIPT = {
    "config_stdout": PCT_CONFIG_RUNNING,
    "status_stdout": PCT_STATUS_RUNNING,
    "pull_rc": 1,
    "script_stdout": "",
}


class ScriptedLxcExecutor:
    """Fake executor for lxc flow tests.

    run_shell() responses are keyed by command substring; first match wins.
    New executor methods (introspect, lxc_os_update, etc.) have dedicated
    configuration parameters and track calls in self.commands.
    snapshot() scripting is separate.
    """

    host = "pve-01"

    def __init__(
        self,
        script=None,
        default=None,
        snap_changed=True,
        introspect_facts=None,
        lxc_os_result=None,
        lxc_app_result=None,
        post_update_facts=None,
    ):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default if default is not None else _ok()
        self.commands: list = []
        self.snap_changed = snap_changed
        self.snapshots_created: list = []
        self.snapshots_deleted: list = []
        self.rollback_called = False

        self._introspect_facts = introspect_facts if introspect_facts is not None else dict(_INTROSPECT_NO_SCRIPT)
        self._lxc_os_result = lxc_os_result
        self._lxc_app_result = lxc_app_result if lxc_app_result is not None else _ok()
        self._post_update_facts = post_update_facts if post_update_facts is not None else {
            "dpkg_hash_after": "",
            "version_after": "",
        }

    def _resp(self, command):
        for key, queue in self.script.items():
            if key in command and queue:
                return queue.pop(0)
        return self.default

    def run_shell(self, command, **opts):
        self.commands.append(command)
        return self._resp(command)

    def run_local(self, command):
        return self._resp(command)

    def reboot(self, *, timeout=600):
        return _ok()

    def snapshot(self, lxc_id, *, snap_state, **api_params):
        if snap_state == "present":
            self.snapshots_created.append(lxc_id)
        else:
            self.snapshots_deleted.append(lxc_id)
        return _ok(changed=self.snap_changed)

    def introspect(self, lxc_id):
        self.commands.append(f"introspect:{lxc_id}")
        return PrimitiveResult(
            rc=0, stdout="", stderr="", changed=False, failed=False,
            facts=dict(self._introspect_facts),
        )

    def vzdump(self, lxc_id, *, backup_storage, lxc_name):
        self.commands.append(f"vzdump {lxc_id}")
        return _ok()

    def lxc_os_update(self, lxc_id, *, os_update_cmd):
        self.commands.append(os_update_cmd)  # preserves LC_ALL=C assertions
        if self._lxc_os_result is not None:
            return self._lxc_os_result
        return self._resp(os_update_cmd)

    def lxc_app_update(
        self, lxc_id, *, lxc_shell="bash", lxc_unattended=True,
        lxc_needs_scale=False, lxc_build_cpu="", lxc_build_ram="",
        lxc_run_cpu="", lxc_run_ram="",
    ):
        self.commands.append(f"lxc_app_update:{lxc_id}")
        return self._lxc_app_result

    def post_update(self, lxc_id, *, lxc_shell="bash", dpkg_hash_cmd="", lxc_script_name=""):
        self.commands.append(f"post_update:{lxc_id}")
        return PrimitiveResult(
            rc=0, stdout="", stderr="", changed=False, failed=False,
            facts=dict(self._post_update_facts),
        )

    def pct_rollback(self, lxc_id):
        self.commands.append(f"pct rollback {lxc_id}")
        self.rollback_called = True
        return _ok()

    def pct_start(self, lxc_id):
        self.commands.append(f"pct start {lxc_id}")
        return _ok()

    def pct_stop(self, lxc_id):
        self.commands.append(f"pct stop {lxc_id}")
        return _ok()


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
    return ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_WITH_SCRIPT,
        script={
            # ver_before: still run_shell via _read_version()
            "cat ~/.sonarr": [_ok(stdout=ver_before)],
            # dpkg_before: still run_shell
            "dpkg-query": [_ok(stdout="hash_before  -\n", changed=False)],
            # reboot check — no reboot needed
            "test -f /var/run/reboot-required": [_fail(rc=1)],
        },
        lxc_os_result=_ok(stdout=os_stdout, changed=True),
        post_update_facts={
            "dpkg_hash_after": "hash_before  -\n",  # same hash → no package change
            "version_after": ver_after,
        },
    )


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
    monkeypatch.setattr(
        http_mod, "request",
        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_WITH_REPO),
    )
    monkeypatch.setattr(http_mod, "get_json", lambda url, **kw: {"tag_name": "v1.5"})
    ex = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_WITH_SCRIPT,
        script={"cat ~/.sonarr": [_ok(stdout="1.4")]},
    )
    out = run_lxc_update("pve-01", "101", ex, _settings(), dry_run=True, api_host="192.168.1.10")
    assert out.record is not None
    assert "→" in out.record.app
    assert out.changed is False
    # No OS update or app update ran
    assert not any("apt-get" in c for c in ex.commands)
    assert not any("lxc_app_update" in c for c in ex.commands)
    assert ex.snapshots_created == []


def test_no_update_script(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ex = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_NO_SCRIPT,
        script={"reboot-required": [_fail(rc=1)]},
        lxc_os_result=_ok(stdout="0 upgraded, 0 newly installed"),
    )
    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert out.record is not None
    assert out.record.app == "NO SCRIPT"
    assert out.failed is False


def test_template_skipped(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ex = ScriptedLxcExecutor(
        introspect_facts={
            "config_stdout": PCT_CONFIG_TEMPLATE,
            "status_stdout": PCT_STATUS_RUNNING,
            "pull_rc": 1,
            "script_stdout": "",
        },
    )
    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert out.record is None
    assert out.failed is False
    # Only introspect was called; no mutations
    assert len(ex.commands) == 1
    assert "introspect" in ex.commands[0]
    assert ex.snapshots_created == []


# ---------------------------------------------------------------------------
# Snapshot / rollback
# ---------------------------------------------------------------------------


def test_snapshot_rollback_on_failure(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))

    class FailingExecutor(ScriptedLxcExecutor):
        def lxc_os_update(self, lxc_id, *, os_update_cmd):
            self.commands.append(os_update_cmd)
            raise RuntimeError("apt-get failed")

        def run_shell(self, command, **opts):
            self.commands.append(command)
            # Return running status once rollback is done (for the polling loop)
            if "pct status" in command and self.rollback_called:
                return _ok(stdout=PCT_STATUS_RUNNING)
            return self._resp(command)

    ex = FailingExecutor(
        introspect_facts=_INTROSPECT_WITH_SCRIPT,
        script={"cat ~/.sonarr": [_ok(stdout="1.0")]},
        snap_changed=True,
    )

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
        def lxc_os_update(self, lxc_id, *, os_update_cmd):
            self.commands.append(os_update_cmd)
            raise RuntimeError("apt-get failed")

    ex = FailingExecutor(
        introspect_facts=_INTROSPECT_WITH_SCRIPT,
        script={"cat ~/.sonarr": [_ok(stdout="1.0")]},
    )
    settings = _settings(lxc_backup_strategy="none")

    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert out.failed is True
    # strategy=none → no snapshot attempted → plain "FAILED"
    assert out.record.app == "FAILED"
    assert ex.rollback_called is False
    assert ex.snapshots_created == []


def test_snapshot_failure_warning(monkeypatch):
    """snapshot() returns changed=False → warning appended, update continues."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_NO_SCRIPT,
        script={"reboot-required": [_fail(rc=1)]},
        lxc_os_result=_ok(stdout="0 upgraded, 0 newly installed"),
        snap_changed=False,
    )

    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert len(out.warnings) >= 1
    assert any("snapshot" in w.warning.lower() for w in out.warnings)
    assert out.failed is False


def test_vzdump_failure_triggers_rescue(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))

    class VzdumpFailExecutor(ScriptedLxcExecutor):
        def vzdump(self, lxc_id, *, backup_storage, lxc_name):
            self.commands.append(f"vzdump {lxc_id}")
            raise RuntimeError("vzdump failed")

    ex = VzdumpFailExecutor(introspect_facts=_INTROSPECT_WITH_SCRIPT)
    settings = _settings(lxc_backup_strategy="vzdump")

    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert out.failed is True
    # vzdump runs BEFORE snapshot; no snapshot taken (snap_taken=False,
    # snapshot_failed=False) → plain "FAILED"
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
    ex = ScriptedLxcExecutor(
        introspect_facts={
            "config_stdout": PCT_CONFIG_RUNNING,
            "status_stdout": PCT_STATUS_STOPPED,
            "pull_rc": 1,
            "script_stdout": "",
        },
        script={
            # re-check status after pct_start (still run_shell)
            "pct status": [_ok(stdout=PCT_STATUS_RUNNING)],
            "reboot-required": [_fail(rc=1)],
        },
        lxc_os_result=_ok(stdout="0 upgraded, 0 newly installed"),
    )
    run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert any("pct start" in c for c in ex.commands)
    assert any("pct stop" in c for c in ex.commands)


# ---------------------------------------------------------------------------
# OS update excluded
# ---------------------------------------------------------------------------


def test_os_excluded(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_NO_SCRIPT,
        script={"reboot-required": [_fail(rc=1)]},
    )
    settings = _settings(os_update_exclude_list=["101"])
    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert not any("apt-get" in c for c in ex.commands)
    if out.record:
        assert out.record.os == "SKIPPED"


# ---------------------------------------------------------------------------
# App update excluded
# ---------------------------------------------------------------------------


def test_app_excluded(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_WITH_SCRIPT,
        script={"reboot-required": [_fail(rc=1)]},
        lxc_os_result=_ok(stdout="2 upgraded, 0 newly installed", changed=True),
    )
    settings = _settings(app_update_exclude_list=["101"])
    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert any("apt-get" in c for c in ex.commands)
    assert not any(c.startswith("lxc_app_update:") for c in ex.commands)
    assert not any(c.startswith("post_update:") for c in ex.commands)
    assert out.record is not None
    assert out.record.app == "SKIPPED"
    assert out.record.os.startswith("Updated")


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
    ex = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_WITH_SCRIPT,
        script={
            # No version file → empty ver_before
            "cat ~/.sonarr": [_ok(stdout="")],
            # dpkg_before hash
            "dpkg-query": [_ok(stdout="hash_before  -\n", changed=False)],
            "reboot-required": [_fail(rc=1)],
        },
        lxc_os_result=_ok(stdout="2 upgraded, 0 newly installed", changed=True),
        post_update_facts={
            "dpkg_hash_after": "hash_after   -\n",  # DIFFERENT → UPDATED
            "version_after": "",
        },
    )
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
    ex = ScriptedLxcExecutor(
        introspect_facts={
            "config_stdout": PCT_CONFIG_ALPINE,
            "status_stdout": PCT_STATUS_RUNNING,
            "pull_rc": 0,
            "script_stdout": "source ct/sonarr.sh",
        },
        script={
            "cat ~/.sonarr": [_ok(stdout="1.0")],  # ver_before only
            "reboot-required": [_fail(rc=1)],
        },
        lxc_os_result=_ok(stdout="OK: upgraded", changed=True),
        post_update_facts={
            "dpkg_hash_after": "",
            "version_after": "1.1",
        },
    )
    out = run_lxc_update("pve-01", "101", ex, _settings(lxc_backup_strategy="none"),
                         api_host="192.168.1.10")
    # ver_before is still read via run_shell with _read_version() → check ash is used
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
    """Reboot check runs only when an update script exists (not lxc_no_update_script)."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_WITH_SCRIPT,
        script={
            "cat ~/.sonarr": [_ok(stdout="1.0")],
            "dpkg-query": [_ok(stdout="hash  -\n", changed=False)],
            # reboot-required file present → rc=0
            "reboot-required": [_ok(rc=0, changed=False)],
            "pct reboot": [_ok(changed=True)],
        },
        lxc_os_result=_ok(stdout="2 upgraded, 0 newly installed", changed=True),
        post_update_facts={
            "dpkg_hash_after": "hash  -\n",
            "version_after": "1.1",
        },
    )
    settings = _settings(lxc_auto_reboot=True, lxc_backup_strategy="none")
    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert out.record is not None
    assert "Rebooted" in out.record.os


# ---------------------------------------------------------------------------
# snapshot_with_retry
# ---------------------------------------------------------------------------

_API = {"api_host": "1.2.3.4", "api_user": "root@pam", "api_token_id": "tok", "api_token_secret": "sec"}


class _QueueSnapshotEx:
    """Minimal executor whose snapshot() dequeues pre-scripted results."""
    host = "pve-01"

    def __init__(self, results):
        self._queue = list(results)
        self.calls: list = []

    def snapshot(self, vmid, *, snap_state, **kw):
        self.calls.append(snap_state)
        return self._queue.pop(0)


def test_snapshot_with_retry_succeeds_after_two_failures():
    from proxmox_fleet.executor import snapshot_with_retry
    ex = _QueueSnapshotEx([
        PrimitiveResult(rc=1, failed=True, changed=False, stdout="CT is locked"),
        PrimitiveResult(rc=1, failed=True, changed=False, stdout="CT is locked"),
        _ok(changed=True),
    ])
    result = snapshot_with_retry(ex, "101", snap_state="present", **_API,
                                 retries=3, _sleep=lambda s: None)
    assert result.changed is True
    assert len(ex.calls) == 3


def test_snapshot_with_retry_returns_failed_after_exhaustion():
    from proxmox_fleet.executor import snapshot_with_retry
    ex = _QueueSnapshotEx([
        PrimitiveResult(rc=1, failed=True, changed=False, stdout="CT is locked"),
        PrimitiveResult(rc=1, failed=True, changed=False, stdout="CT is locked"),
        PrimitiveResult(rc=1, failed=True, changed=False, stdout="CT is locked"),
    ])
    result = snapshot_with_retry(ex, "101", snap_state="present", **_API,
                                 retries=2, _sleep=lambda s: None)
    assert result.failed is True
    assert result.changed is False
    assert len(ex.calls) == 3  # initial + 2 retries


# ---------------------------------------------------------------------------
# _discover_lxcs — os_only_lxc_list union
# ---------------------------------------------------------------------------


def test_discover_lxcs_includes_os_only_ids():
    ex = ScriptedLxcExecutor(default=_ok(stdout="101\n105\n"))
    settings = _settings(os_only_lxc_list=["105"])
    ids = _discover_lxcs(ex, settings)
    assert "grep -qxE '(105)'" in ex.commands[0]
    assert ids == ["101", "105"]


def test_discover_lxcs_no_os_only_list_keeps_command_unchanged():
    ex = ScriptedLxcExecutor(default=_ok(stdout="101\n"))
    settings = _settings()
    _discover_lxcs(ex, settings)
    assert "grep -qxE" not in ex.commands[0]


def test_discover_lxcs_exclude_list_wins_over_os_only_list():
    ex = ScriptedLxcExecutor(default=_ok(stdout="101\n105\n"))
    settings = _settings(os_only_lxc_list=["105"], exclude_list=["105"])
    ids = _discover_lxcs(ex, settings)
    assert ids == ["101"]
