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
        self.app_update_kwargs: dict = {}
        self.snapshot_api_params: list = []

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
        self.snapshot_api_params.append(dict(api_params))
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
        self.app_update_kwargs = {
            "lxc_needs_scale": lxc_needs_scale,
            "lxc_build_cpu": lxc_build_cpu, "lxc_build_ram": lxc_build_ram,
            "lxc_run_cpu": lxc_run_cpu, "lxc_run_ram": lxc_run_ram,
        }
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


# ---------------------------------------------------------------------------
# Task 3: per-cluster API credentials reach the snapshot call
# ---------------------------------------------------------------------------


def test_snapshot_uses_global_creds_for_default_cluster(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = _exec_normal()
    run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert ex.snapshot_api_params  # at least one snapshot call happened
    for params in ex.snapshot_api_params:
        assert params == {
            "api_host": "192.168.1.10",
            "api_user": "root@pam",
            "api_token_id": "tok",
            "api_token_secret": "secret",
        }


def test_snapshot_uses_beta_cluster_override(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = _exec_normal()
    settings = _settings(pve_clusters={
        "beta": {
            "pve_api_user": "beta-user@pve",
            "pve_api_token_id": "beta-tok",
            "pve_api_token_secret": "beta-secret",
        }
    })
    run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10", cluster="beta")
    assert ex.snapshot_api_params
    for params in ex.snapshot_api_params:
        assert params == {
            "api_host": "192.168.1.10",
            "api_user": "beta-user@pve",
            "api_token_id": "beta-tok",
            "api_token_secret": "beta-secret",
        }


def test_snapshot_uses_globals_for_unconfigured_cluster(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = _exec_normal()
    settings = _settings(pve_clusters={
        "beta": {"pve_api_token_secret": "beta-secret"},
    })
    # cluster="alpha" has no override — falls back to globals, same as default.
    run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10", cluster="alpha")
    assert ex.snapshot_api_params
    for params in ex.snapshot_api_params:
        assert params["api_user"] == "root@pam"
        assert params["api_token_id"] == "tok"
        assert params["api_token_secret"] == "secret"


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


def test_no_update_script_os_only_idle_suppressed(monkeypatch):
    # os_only_lxc_list container (script expected to be missing) + idle OS → no record
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ex = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_NO_SCRIPT,
        script={"reboot-required": [_fail(rc=1)]},
        lxc_os_result=_ok(stdout="0 upgraded, 0 newly installed"),
    )
    settings = _settings(os_only_lxc_list=["101"])
    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert out.record is None
    assert out.failed is False


def test_no_update_script_unexpected_is_reported(monkeypatch):
    # Tagged container whose script pull failed unexpectedly → anomaly, record emitted
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


def test_no_update_script_os_updated(monkeypatch):
    # NO SCRIPT + OS changed → record emitted even for os_only containers
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ex = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_NO_SCRIPT,
        script={"reboot-required": [_fail(rc=1)]},
        lxc_os_result=_ok(stdout="1 upgraded, 0 newly installed, 0 to remove and 0 not upgraded."),
    )
    settings = _settings(os_only_lxc_list=["101"])
    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert out.record is not None
    assert out.record.app == "NO SCRIPT"
    assert out.failed is False


def test_no_update_script_os_only_reboots_after_kernel_update(monkeypatch):
    # The reboot-required check must run even without an update script —
    # os_only_lxc_list containers still get kernel updates.
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ex = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_NO_SCRIPT,
        script={
            "reboot-required": [_ok(rc=0, changed=False)],
            "pct reboot": [_ok(changed=True)],
        },
        lxc_os_result=_ok(stdout="5 upgraded, 0 newly installed", changed=True),
    )
    settings = _settings(os_only_lxc_list=["101"], lxc_auto_reboot=True,
                         lxc_backup_strategy="none")
    out = run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10")
    assert out.record is not None
    assert "Rebooted" in out.record.os
    assert any("pct reboot" in c for c in ex.commands)


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
    """A reboot-required file plus lxc_auto_reboot yields the '& Rebooted' suffix."""
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


def test_snapshot_with_retry_returns_failed_when_snapshot_raises():
    # Contract: a failed snapshot is a warning, never a raise — even when the
    # primitive invocation itself throws instead of returning failed=True.
    from proxmox_fleet.executor import snapshot_with_retry

    class _RaisingEx:
        host = "pve-01"

        def snapshot(self, vmid, *, snap_state, **kw):
            raise OSError("ansible-runner exploded")

    result = snapshot_with_retry(_RaisingEx(), "101", snap_state="present", **_API,
                                 retries=2, _sleep=lambda s: None)
    assert result.failed is True
    assert result.changed is False


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


def test_discover_lxcs_unreachable_raises_typed_error():
    import pytest
    from proxmox_fleet.runner import PrimitiveResult, UnreachableHostError

    ex = ScriptedLxcExecutor(default=PrimitiveResult(
        rc=4, failed=True, unreachable=True, stderr="ssh: No route to host"))
    with pytest.raises(UnreachableHostError, match="No route to host"):
        _discover_lxcs(ex, _settings())


def test_discover_lxcs_plain_failure_raises_runtime_error():
    import pytest
    from proxmox_fleet.runner import PrimitiveResult, UnreachableHostError

    ex = ScriptedLxcExecutor(default=PrimitiveResult(
        rc=1, failed=True, stderr="pct: command not found"))
    with pytest.raises(RuntimeError) as exc_info:
        _discover_lxcs(ex, _settings())
    assert not isinstance(exc_info.value, UnreachableHostError)


# ---------------------------------------------------------------------------
# Non-raising update failures — captured output + failed run
# ---------------------------------------------------------------------------


def test_app_update_failure_records_error_and_fails_run(monkeypatch):
    """A non-zero /usr/bin/update must carry its output out, not just a boolean."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = _exec_normal()
    ex._lxc_app_result = _fail(rc=1, stderr="npm: migration failed, aborting")
    out = run_lxc_update("pve-01", "123", ex, _settings(), api_host="192.168.1.10")

    assert out.failed is True
    assert out.record is not None and out.record.app == "FAILED"
    assert len(out.errors) == 1
    assert out.errors[0].host == "123"
    assert out.errors[0].task == "app update"
    assert "migration failed" in out.errors[0].error


def test_os_update_failure_records_error_and_fails_run(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = _exec_normal()
    ex._lxc_os_result = _fail(rc=100, stderr="E: You don't have enough free space in /var/cache/apt")
    out = run_lxc_update("pve-01", "130", ex, _settings(), api_host="192.168.1.10")

    assert out.failed is True
    assert out.record is not None and out.record.os == "FAILED"
    assert [e.task for e in out.errors] == ["OS update"]
    assert "enough free space" in out.errors[0].error


def test_both_updates_failing_record_one_error_each(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = _exec_normal()
    ex._lxc_os_result = _fail(rc=100, stderr="E: no space left on device")
    ex._lxc_app_result = _fail(rc=1, stderr="build step exited 1")
    out = run_lxc_update("pve-01", "130", ex, _settings(), api_host="192.168.1.10")

    assert out.failed is True
    assert [e.task for e in out.errors] == ["OS update", "app update"]


def test_failure_detail_falls_back_to_stdout_then_rc(monkeypatch):
    from proxmox_fleet.flows.lxc import _failure_detail

    assert _failure_detail(PrimitiveResult(rc=1, failed=True, stderr="  boom  ")) == "boom"
    # stdout is used when stderr is empty, and newlines are collapsed for the
    # briefing's inline-code rendering
    assert _failure_detail(
        PrimitiveResult(rc=1, failed=True, stdout="line one\nline two")
    ) == "line one line two"
    assert "rc=7" in _failure_detail(PrimitiveResult(rc=7, failed=True))


def test_failure_detail_keeps_the_tail_of_long_output():
    from proxmox_fleet.flows.lxc import _FAILURE_DETAIL_MAX, _failure_detail

    long = "x" * 500 + " E: the actual complaint"
    detail = _failure_detail(PrimitiveResult(rc=1, failed=True, stderr=long))
    assert detail.startswith("...")
    assert detail.endswith("E: the actual complaint")
    assert len(detail) == _FAILURE_DETAIL_MAX + 3


def test_successful_run_records_no_errors(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    ex = _exec_normal(ver_before="1.0", ver_after="1.1")
    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert out.failed is False
    assert out.errors == []


def test_failure_detail_strips_ansi_and_keeps_the_real_cause():
    """Verbatim stderr from a live exit-203 run of ct/nginxproxymanager.sh.

    The last line is the misleading fallthrough into build_container; the actual
    cause is two lines above it, so both must survive stripping + truncation.
    """
    from proxmox_fleet.flows.lxc import _failure_detail

    stderr = (
        "\x1b[K  ✖️  \x1b[01;31mContainer OS debian 12 does not match the "
        "recommended debian 13 — skipping update.\x1b[m\n"
        "\x1b[K  ✖️  \x1b[01;31mUpgrade the container OS to debian 13 first, then "
        "run this update again — or bypass this check (may break, no support) with: "
        'echo "debian 13" > /usr/local/community-scripts/ignore-os-mismatch\x1b[m\n'
        "\x1b[K  ✖️  \x1b[01;31mYou need to set 'CTID' variable.\x1b[m\n"
    )
    detail = _failure_detail(PrimitiveResult(rc=203, failed=True, stderr=stderr))

    assert "\x1b" not in detail
    assert "[K" not in detail
    assert "Container OS debian 12 does not match the recommended debian 13" in detail
    assert "You need to set 'CTID' variable." in detail


def test_failure_detail_prefers_stderr_over_banner_stdout():
    """The community scripts put their banner on stdout and the error on stderr."""
    from proxmox_fleet.flows.lxc import _failure_detail

    detail = _failure_detail(PrimitiveResult(
        rc=203, failed=True,
        stdout="   ____  _   _ ___ \n  |  _ \\| \\ | |  _ \\ \n",
        stderr="\x1b[01;31mContainer OS debian 12 does not match\x1b[m",
    ))
    assert detail == "Container OS debian 12 does not match"


# ---------------------------------------------------------------------------
# Pre-emptive health warnings (low disk / OS behind the ct script's target)
# ---------------------------------------------------------------------------

# Verbatim `df -P /` from CT 130 (grafana) the morning its update failed.
DF_90_PERCENT = (
    "Filesystem     1024-blocks    Used Available Capacity Mounted on\n"
    "/dev/rbd17         4046560 3416796    403668      90% /\n"
)
DF_52_PERCENT = (
    "Filesystem     1024-blocks    Used Available Capacity Mounted on\n"
    "/dev/rbd7         17369872 8440172   8022028      52% /\n"
)
# Verbatim /etc/os-release from CT 123 (nginxproxymanager).
OS_RELEASE_BOOKWORM = (
    'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'
    'NAME="Debian GNU/Linux"\n'
    'VERSION_ID="12"\n'
    'VERSION="12 (bookworm)"\n'
    "VERSION_CODENAME=bookworm\n"
    "ID=debian\n"
)
OS_RELEASE_TRIXIE = (
    'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n'
    'VERSION_ID="13"\n'
    "ID=debian\n"
)
# Verbatim var_* block from the current ct/nginxproxymanager.sh.
CT_SCRIPT_TRIXIE = (
    'var_cpu="${var_cpu:-2}"\n'
    'var_ram="${var_ram:-2048}"\n'
    'var_disk="${var_disk:-12}"\n'
    'var_os="${var_os:-debian}"\n'
    'var_version="${var_version:-13}"\n'
)


def _exec_with_health(df="", os_release=""):
    """Happy-path executor whose introspect also returns the two health facts.

    The ct script body itself comes from the monkeypatched http_mod.request.
    """
    ex = _exec_normal()
    ex._introspect_facts = dict(
        _INTROSPECT_WITH_SCRIPT, df_stdout=df, os_release_stdout=os_release)
    return ex


def test_disk_warning_fires_at_threshold(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_TRIXIE))
    ex = _exec_with_health(df=DF_90_PERCENT, os_release=OS_RELEASE_TRIXIE)
    out = run_lxc_update("pve-01", "130", ex, _settings(), api_host="192.168.1.10")

    disk = [w for w in out.warnings if w.task == "disk space"]
    assert len(disk) == 1
    assert disk[0].host == "130"
    assert "90% full" in disk[0].warning


def test_disk_warning_silent_below_threshold(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_TRIXIE))
    ex = _exec_with_health(df=DF_52_PERCENT, os_release=OS_RELEASE_TRIXIE)
    out = run_lxc_update("pve-01", "105", ex, _settings(), api_host="192.168.1.10")
    assert [w for w in out.warnings if w.task == "disk space"] == []


def test_disk_threshold_is_configurable(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_TRIXIE))
    ex = _exec_with_health(df=DF_52_PERCENT, os_release=OS_RELEASE_TRIXIE)
    settings = _settings(lxc_disk_warn_percent=50)
    out = run_lxc_update("pve-01", "105", ex, settings, api_host="192.168.1.10")
    assert [w.task for w in out.warnings if w.task == "disk space"] == ["disk space"]


def test_os_mismatch_warning_fires_for_bookworm_on_a_trixie_script(monkeypatch):
    """CT 123's exact situation: debian 12 container, ct script targeting 13."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_TRIXIE))
    ex = _exec_with_health(df=DF_52_PERCENT, os_release=OS_RELEASE_BOOKWORM)
    out = run_lxc_update("pve-01", "123", ex, _settings(), api_host="192.168.1.10")

    osw = [w for w in out.warnings if w.task == "container OS"]
    assert len(osw) == 1
    assert "debian 12" in osw[0].warning and "debian 13" in osw[0].warning
    assert "203" in osw[0].warning


def test_os_mismatch_silent_when_versions_agree(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_TRIXIE))
    ex = _exec_with_health(df=DF_52_PERCENT, os_release=OS_RELEASE_TRIXIE)
    out = run_lxc_update("pve-01", "125", ex, _settings(), api_host="192.168.1.10")
    assert [w for w in out.warnings if w.task == "container OS"] == []


def test_health_warnings_are_emitted_on_the_dry_run_path(monkeypatch):
    """The point of the warnings: arrive before the window, not with the failure."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_TRIXIE))
    monkeypatch.setattr(http_mod, "get_json", lambda url, **kw: {"tag_name": "1.1"})
    ex = _exec_with_health(df=DF_90_PERCENT, os_release=OS_RELEASE_BOOKWORM)
    out = run_lxc_update("pve-01", "123", ex, _settings(), dry_run=True,
                         api_host="192.168.1.10")

    assert sorted(w.task for w in out.warnings) == ["container OS", "disk space"]
    assert ex.snapshots_created == []  # still a dry run


def test_health_warnings_survive_a_failing_update(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_TRIXIE))
    ex = _exec_with_health(df=DF_90_PERCENT, os_release=OS_RELEASE_TRIXIE)
    ex._lxc_app_result = _fail(rc=114, stderr="Storage too low")
    out = run_lxc_update("pve-01", "130", ex, _settings(), api_host="192.168.1.10")

    assert out.failed is True
    assert [w.task for w in out.warnings] == ["disk space"]


def test_no_warnings_when_container_was_not_running_at_introspect(monkeypatch):
    """df/os-release come back empty for a stopped CT — must not false-positive."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_TRIXIE))
    ex = _exec_with_health(df="", os_release="")
    out = run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert out.warnings == []


# ---------------------------------------------------------------------------
# Resource scaling — plan always computed, execution opt-in
# ---------------------------------------------------------------------------

PCT_CONFIG_SMALL = "hostname: sonarr\nostype: debian\ncores: 2\nmemory: 2048\n"
CT_SCRIPT_HUNGRY = 'var_cpu="${var_cpu:-4}"\nvar_ram="${var_ram:-6144}"\n'


def _exec_under_provisioned():
    ex = _exec_normal()
    ex._introspect_facts = dict(_INTROSPECT_WITH_SCRIPT, config_stdout=PCT_CONFIG_SMALL)
    return ex


def test_scaling_not_applied_by_default(monkeypatch):
    """lxc_resource_scaling defaults off — upstream no longer scales at build time."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_HUNGRY))
    ex = _exec_under_provisioned()
    run_lxc_update("pve-01", "101", ex, _settings(), api_host="192.168.1.10")
    assert ex.app_update_kwargs["lxc_needs_scale"] is False


def test_scaling_applied_when_enabled(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: http_mod.HttpResponse(200, CT_SCRIPT_HUNGRY))
    ex = _exec_under_provisioned()
    run_lxc_update("pve-01", "101", ex, _settings(lxc_resource_scaling=True),
                   api_host="192.168.1.10")

    kw = ex.app_update_kwargs
    assert kw["lxc_needs_scale"] is True
    assert (kw["lxc_build_cpu"], kw["lxc_build_ram"]) == ("4", "6144")
    # restores the container's own live values, not the script's spec
    assert (kw["lxc_run_cpu"], kw["lxc_run_ram"]) == ("2", "2048")


def test_scaling_inert_when_allocation_already_matches(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request",
                        lambda url, **kw: http_mod.HttpResponse(
                            200, 'var_cpu="${var_cpu:-2}"\nvar_ram="${var_ram:-2048}"\n'))
    ex = _exec_under_provisioned()
    run_lxc_update("pve-01", "101", ex, _settings(lxc_resource_scaling=True),
                   api_host="192.168.1.10")
    assert ex.app_update_kwargs["lxc_needs_scale"] is False


# ---------------------------------------------------------------------------
# Multi-cluster qualified ids (Task 1) — cluster.py wiring
# ---------------------------------------------------------------------------


def test_discover_lxcs_qualified_os_only_matches_its_cluster():
    ex = ScriptedLxcExecutor(default=_ok(stdout="101\n105\n"))
    settings = _settings(os_only_lxc_list=["alpha/105"])
    ids = _discover_lxcs(ex, settings, cluster="alpha")
    # The cluster qualifier must never reach the shell regex — only the bare id.
    assert "grep -qxE '(105)'" in ex.commands[0]
    assert ids == ["101", "105"]


def test_discover_lxcs_qualified_os_only_other_cluster_dropped_before_regex():
    ex = ScriptedLxcExecutor(default=_ok(stdout="101\n"))
    settings = _settings(os_only_lxc_list=["alpha/105"])
    _discover_lxcs(ex, settings, cluster="beta")
    # 105 only applies to "alpha" — beta's regex must not reference it at all.
    assert "grep -qxE" not in ex.commands[0]


def test_discover_lxcs_qualified_exclude_scopes_to_its_cluster():
    ex = ScriptedLxcExecutor(default=_ok(stdout="101\n105\n"))
    settings = _settings(exclude_list=["alpha/105"])
    assert _discover_lxcs(ex, settings, cluster="alpha") == ["101"]

    ex2 = ScriptedLxcExecutor(default=_ok(stdout="101\n105\n"))
    assert _discover_lxcs(ex2, settings, cluster="beta") == ["101", "105"]


def test_discover_lxcs_bare_exclude_applies_to_every_cluster():
    settings = _settings(exclude_list=["105"])
    ex_a = ScriptedLxcExecutor(default=_ok(stdout="101\n105\n"))
    ex_b = ScriptedLxcExecutor(default=_ok(stdout="101\n105\n"))
    assert _discover_lxcs(ex_a, settings, cluster="alpha") == ["101"]
    assert _discover_lxcs(ex_b, settings, cluster="beta") == ["101"]


def test_run_lxc_update_qualified_app_exclude_only_its_cluster(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    settings = _settings(app_update_exclude_list=["alpha/101"])

    ex_alpha = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_WITH_SCRIPT,
        script={"reboot-required": [_fail(rc=1)]},
        lxc_os_result=_ok(stdout="2 upgraded, 0 newly installed", changed=True),
    )
    out_alpha = run_lxc_update("pve-01", "101", ex_alpha, settings,
                               api_host="192.168.1.10", cluster="alpha")
    assert out_alpha.record.app == "SKIPPED"
    assert not any(c.startswith("lxc_app_update:") for c in ex_alpha.commands)

    ex_beta = ScriptedLxcExecutor(
        introspect_facts=_INTROSPECT_WITH_SCRIPT,
        script={"reboot-required": [_fail(rc=1)]},
        lxc_os_result=_ok(stdout="2 upgraded, 0 newly installed", changed=True),
    )
    out_beta = run_lxc_update("pve-01", "101", ex_beta, settings,
                              api_host="192.168.1.10", cluster="beta")
    assert out_beta.record.app != "SKIPPED"
    assert any(c.startswith("lxc_app_update:") for c in ex_beta.commands)


def test_run_lxc_update_bare_app_exclude_applies_to_every_cluster(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    settings = _settings(app_update_exclude_list=["101"])

    for cluster in ("alpha", "beta"):
        ex = ScriptedLxcExecutor(
            introspect_facts=_INTROSPECT_WITH_SCRIPT,
            script={"reboot-required": [_fail(rc=1)]},
            lxc_os_result=_ok(stdout="2 upgraded, 0 newly installed", changed=True),
        )
        out = run_lxc_update("pve-01", "101", ex, settings,
                             api_host="192.168.1.10", cluster=cluster)
        assert out.record.app == "SKIPPED"


def test_run_lxc_update_kuma_map_exact_qualified_key_wins(monkeypatch):
    """map_lookup: an exact "cluster/id" kuma key beats the bare "id" key."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(http_mod, "request", lambda url, **kw: http_mod.HttpResponse(200, ""))
    monkeypatch.setattr(http_mod, "get_json", lambda url, **kw: {"heartbeatList": {}})

    from proxmox_fleet.flows import lxc as lxc_mod

    captured = {}

    def _fake_kuma_healthy(payload, *, monitor_id):
        captured["monitor_id"] = monitor_id
        return True

    monkeypatch.setattr(lxc_mod, "kuma_healthy", _fake_kuma_healthy)

    settings = _settings(
        lxc_kuma_map={"101": "5", "alpha/101": "9"},
        kuma_url="http://kuma", kuma_slug="fleet",
    )
    ex = _exec_normal(ver_before="1.0", ver_after="1.1")
    run_lxc_update("pve-01", "101", ex, settings, api_host="192.168.1.10", cluster="alpha")
    assert captured["monitor_id"] == "9"
