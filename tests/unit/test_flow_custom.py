"""Tests for proxmox_fleet.flows.custom — the custom_update control flow.

Uses a scripted fake executor (no real Ansible) and monkeypatches http for the
GitHub/Kuma calls. Asserts the control flow: rescue/rollback, health gating,
dependency skip, dry-run, outdated gate, and byte-parity status strings.
"""

from proxmox_fleet import http as http_mod
from proxmox_fleet.flows import custom as flow
from proxmox_fleet.models.config import CustomConfig
from proxmox_fleet.runner import PrimitiveResult


def _ok(stdout="", changed=True, rc=0):
    return PrimitiveResult(rc=rc, changed=changed, stdout=stdout, failed=False)


def _fail(rc=1):
    return PrimitiveResult(rc=rc, failed=True, stderr="boom")


class ScriptedExecutor:
    host = "nas-01"

    def __init__(self, script=None, default=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default if default is not None else _ok()
        self.commands = []
        self.local_commands = []
        self.reboots = 0

    def _resp(self, command):
        for key, queue in self.script.items():
            if key in command and queue:
                return queue.pop(0)
        return self.default

    def run_shell(self, command, **opts):
        self.commands.append(command)
        return self._resp(command)

    def run_local(self, command):
        self.local_commands.append(command)
        return self._resp(command)

    def reboot(self, *, timeout=600):
        self.reboots += 1
        return _ok()


def _cfg(**overrides):
    base = {
        "name": "Gitea",
        "version_command": "gitea --version",
        "update_steps": [{"name": "upgrade", "command": "do-upgrade"}],
        "changed_when": {"type": "version"},
    }
    base.update(overrides)
    return CustomConfig.model_validate(base)


# --- happy paths ---------------------------------------------------------------

def test_version_change_records_updated():
    ex = ScriptedExecutor(script={"gitea --version": [_ok(stdout="1.0"), _ok(stdout="1.1")]})
    out = flow.run_custom_update("nas-01", _cfg(), ex)
    assert out.failed is False
    assert out.changed is True
    assert out.record.app == "Updated: 1.0 → 1.1"
    assert out.should_report is True


def test_noop_when_version_unchanged():
    ex = ScriptedExecutor(script={"gitea --version": [_ok(stdout="1.0"), _ok(stdout="1.0")]})
    out = flow.run_custom_update("nas-01", _cfg(), ex)
    assert out.changed is False
    assert out.record is None  # idle OK is suppressed
    assert out.should_report is False


def test_dry_run_reports_versions(monkeypatch):
    monkeypatch.setattr(http_mod, "get_json", lambda url, **kw: {"tag_name": "v1.5"})
    cfg = _cfg(latest_version={"type": "github_release", "repo": "go-gitea/gitea"})
    ex = ScriptedExecutor(script={"gitea --version": [_ok(stdout="1.4")]})
    out = flow.run_custom_update("nas-01", cfg, ex, dry_run=True)
    assert out.record.app == "dry-run: 1.4 → v1.5"
    assert out.should_report is True
    assert out.changed is False
    assert ex.commands == ["gitea --version"]  # no update steps ran


def test_reboot_appends_suffix():
    ex = ScriptedExecutor(script={"gitea --version": [_ok(stdout="1.0"), _ok(stdout="1.1")]})
    out = flow.run_custom_update("nas-01", _cfg(reboot=True), ex)
    assert ex.reboots == 1
    assert out.record.app == "Updated: 1.0 → 1.1 + Rebooted"


def test_no_reboot_when_unchanged():
    ex = ScriptedExecutor(script={"gitea --version": [_ok(stdout="1.0"), _ok(stdout="1.0")]})
    flow.run_custom_update("nas-01", _cfg(reboot=True), ex)
    assert ex.reboots == 0


# --- rescue / rollback ---------------------------------------------------------

def test_step_failure_triggers_rollback_and_records_failed():
    ex = ScriptedExecutor(script={
        "gitea --version": [_ok(stdout="1.0")],
        "do-upgrade": [_fail()],
    })
    cfg = _cfg(rollback_command="restore-backup")
    out = flow.run_custom_update("nas-01", cfg, ex)
    assert out.failed is True
    assert out.record.app == "FAILED"
    assert out.error is not None
    assert out.error.task == "upgrade"  # the failing step name
    assert "restore-backup" in ex.commands


def test_health_check_failure_triggers_rescue(monkeypatch):
    # Kuma never goes healthy → poll raises → rescue.
    monkeypatch.setattr(http_mod, "get_json",
                        lambda url, **kw: {"heartbeatList": {"7": [{"status": 0}]}})
    cfg = _cfg(
        health_check={"type": "kuma", "kuma_slug": "gitea", "kuma_monitor_id": "7"},
    )
    ex = ScriptedExecutor(script={"gitea --version": [_ok(stdout="1.0"), _ok(stdout="1.1")]})
    out = flow.run_custom_update("nas-01", cfg, ex, kuma_url="http://kuma", kuma_retries=1, kuma_delay=0)
    assert out.failed is True
    assert out.record.app == "FAILED"


def test_health_check_passes_when_healthy(monkeypatch):
    monkeypatch.setattr(http_mod, "get_json",
                        lambda url, **kw: {"heartbeatList": {"7": [{"status": 1}]}})
    cfg = _cfg(health_check={"type": "kuma", "kuma_slug": "gitea", "kuma_monitor_id": "7"})
    ex = ScriptedExecutor(script={"gitea --version": [_ok(stdout="1.0"), _ok(stdout="1.1")]})
    out = flow.run_custom_update("nas-01", cfg, ex, kuma_url="http://kuma", kuma_retries=1, kuma_delay=0)
    assert out.failed is False
    assert out.record.app == "Updated: 1.0 → 1.1"


# --- gating --------------------------------------------------------------------

def test_dependency_skip_records_warning_only():
    ex = ScriptedExecutor()
    out = flow.run_custom_update("nas-01", _cfg(), ex, dep_failed=True)
    assert out.record is None
    assert out.failed is False
    assert len(out.warnings) == 1
    assert "dependency" in out.warnings[0].warning.lower()
    assert ex.commands == []  # nothing ran


def test_outdated_gate_skips_when_current(monkeypatch):
    monkeypatch.setattr(http_mod, "get_json", lambda url, **kw: {"tag_name": "1.0"})
    cfg = _cfg(
        update_only_if_outdated=True,
        latest_version={"type": "github_release", "repo": "go-gitea/gitea"},
    )
    ex = ScriptedExecutor(script={"gitea --version": [_ok(stdout="1.0")]})
    out = flow.run_custom_update("nas-01", cfg, ex)
    assert out.record is None  # 'OK (up to date)' is not reportable
    assert "do-upgrade" not in ex.commands  # update skipped


def test_outdated_gate_updates_when_behind(monkeypatch):
    monkeypatch.setattr(http_mod, "get_json", lambda url, **kw: {"tag_name": "1.1"})
    cfg = _cfg(
        update_only_if_outdated=True,
        latest_version={"type": "github_release", "repo": "go-gitea/gitea"},
    )
    ex = ScriptedExecutor(script={"gitea --version": [_ok(stdout="1.0"), _ok(stdout="1.1")]})
    out = flow.run_custom_update("nas-01", cfg, ex)
    assert "do-upgrade" in ex.commands
    assert out.record.app == "Updated: 1.0 → 1.1"


def test_outdated_gate_fail_open_warns(monkeypatch):
    # latest unresolved → update anyway + warning
    monkeypatch.setattr(http_mod, "get_json", lambda url, **kw: {})
    cfg = _cfg(
        update_only_if_outdated=True,
        latest_version={"type": "github_release", "repo": "go-gitea/gitea"},
    )
    ex = ScriptedExecutor(script={"gitea --version": [_ok(stdout="1.0"), _ok(stdout="1.1")]})
    out = flow.run_custom_update("nas-01", cfg, ex)
    assert any("fail-open" in w.warning for w in out.warnings)
    assert "do-upgrade" in ex.commands


# --- command health check ----------------------------------------------------

def test_health_check_command_passes():
    cfg = _cfg(
        health_check={"type": "command", "command": "check-alive"},
    )
    ex = ScriptedExecutor(
        script={
            "gitea --version": [_ok(stdout="1.0"), _ok(stdout="1.1")],
            "check-alive": [_ok(rc=0)],
        }
    )
    out = flow.run_custom_update("nas-01", cfg, ex)
    assert out.failed is False
    assert "check-alive" in ex.commands


def test_health_check_command_fails_triggers_rescue():
    cfg = _cfg(
        health_check={"type": "command", "command": "check-alive"},
    )
    ex = ScriptedExecutor(
        script={
            "gitea --version": [_ok(stdout="1.0"), _ok(stdout="1.1")],
            "check-alive": [_fail(rc=1)],
        }
    )
    out = flow.run_custom_update("nas-01", cfg, ex)
    assert out.failed is True
    assert out.record.app == "FAILED"
