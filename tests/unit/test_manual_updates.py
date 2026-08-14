"""Tests for proxmox_fleet.manual_updates — read-only manual update checks.

Covers the TrueNAS SCALE and OPNsense adapters, the registry (including the
unknown-adapter path), command safety invariants (no apt/apply/upgrade,
``opnsense-update -c`` only), ``changed_when=False`` execution, unreachable
normalization (result flag, raised UnreachableHostError, recognized SSH text),
and the always-full-shaped result contract. All executors are scripted fakes —
no live host is ever contacted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from proxmox_fleet.executor import Executor
from proxmox_fleet.manual_updates import (
    MANUAL_UPDATE_REGISTRY,
    BaseManualUpdateAdapter,
    ManualUpdateAdapterError,
    ManualUpdateRegistry,
    ManualUpdateResult,
    OpnsenseAdapter,
    TrueNASScaleAdapter,
    UnknownManualUpdateAdapterError,
    run_manual_update_check,
)
from proxmox_fleet.runner import PrimitiveResult, UnreachableHostError

DATA_DIR = Path(__file__).parent / "data" / "manual_updates"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(name: str) -> str:
    return (DATA_DIR / name).read_text()


def _ok(stdout: str = "", rc: int = 0, stderr: str = "") -> PrimitiveResult:
    return PrimitiveResult(rc=rc, changed=False, stdout=stdout, stderr=stderr, failed=False)


def _unreachable_ok(stdout: str = "", stderr: str = "") -> PrimitiveResult:
    """Ansible flagged the host unreachable: command never ran, rc stays 0."""
    return PrimitiveResult(
        rc=0, changed=False, stdout=stdout, stderr=stderr, failed=True, unreachable=True
    )


class ScriptedManualExecutor(Executor):
    """Scripted fake Executor: one queued response per command substring.

    ``raises`` maps a command substring to an exception to raise instead of a
    response. Every run_shell call is recorded with its kwargs so tests can
    assert on ``changed_when`` and command safety. The LXC/VM methods of the
    Executor protocol are stubbed out — manual update checks never call them.
    """

    host = "manual-host"

    def __init__(
        self,
        script: Optional[Dict[str, List[PrimitiveResult]]] = None,
        default: Optional[PrimitiveResult] = None,
        raises: Optional[Dict[str, Exception]] = None,
    ) -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default if default is not None else _ok()
        self.raises = raises or {}
        self.commands: List[str] = []
        self.opts: List[Dict[str, Any]] = []

    def _resp(self, command: str) -> PrimitiveResult:
        for key, exc in self.raises.items():
            if key in command:
                raise exc
        for key, queue in self.script.items():
            if key in command and queue:
                return queue.pop(0)
        return self.default

    def run_shell(self, command: str, **opts: Any) -> PrimitiveResult:
        self.commands.append(command)
        self.opts.append(opts)
        return self._resp(command)

    def run_local(self, command: str) -> PrimitiveResult:
        return self._resp(command)

    def reboot(self, *, timeout: int = 600) -> PrimitiveResult:
        raise AssertionError("reboot should never be called for manual update checks")

    def node_post_upgrade(self, *, nvidia_host: bool = False) -> PrimitiveResult:
        raise AssertionError("node_post_upgrade should never be called for manual update checks")

    def snapshot(self, vmid: str, *, snap_state: str, **api_params: Any) -> PrimitiveResult:
        raise AssertionError("snapshot should never be called for manual update checks")

    def introspect(self, lxc_id: str) -> PrimitiveResult:
        raise AssertionError("LXC methods should never be called for manual update checks")

    def vzdump(self, lxc_id: str, *, backup_storage: str, lxc_name: str) -> PrimitiveResult:
        raise AssertionError("LXC methods should never be called for manual update checks")

    def lxc_os_update(self, lxc_id: str, *, os_update_cmd: str) -> PrimitiveResult:
        raise AssertionError("LXC methods should never be called for manual update checks")

    def lxc_app_update(self, lxc_id: str, **opts: Any) -> PrimitiveResult:
        raise AssertionError("LXC methods should never be called for manual update checks")

    def post_update(self, lxc_id: str, **opts: Any) -> PrimitiveResult:
        raise AssertionError("LXC methods should never be called for manual update checks")

    def pct_rollback(self, lxc_id: str) -> PrimitiveResult:
        raise AssertionError("LXC methods should never be called for manual update checks")

    def pct_start(self, lxc_id: str) -> PrimitiveResult:
        raise AssertionError("LXC methods should never be called for manual update checks")

    def pct_stop(self, lxc_id: str) -> PrimitiveResult:
        raise AssertionError("LXC methods should never be called for manual update checks")


def _truenas_fixture(name: str) -> ScriptedManualExecutor:
    return ScriptedManualExecutor(script={"midclt": [_ok(stdout=_load(name))]})


def _opnsense_fixture(name: str, rc: int = 0, failed: bool = False) -> ScriptedManualExecutor:
    res = PrimitiveResult(rc=rc, changed=False, stdout=_load(name), failed=failed)
    return ScriptedManualExecutor(script={"opnsense-update": [res]})


# ---------------------------------------------------------------------------
# TrueNAS SCALE parser
# ---------------------------------------------------------------------------


def test_truenas_available() -> None:
    """AVAILABLE status → update available with current/latest/train."""
    result = run_manual_update_check("truenas_scale", _truenas_fixture("truenas_available.txt"), "nas-01")

    assert not result.error
    assert result.unreachable is False
    assert result.update_available is True
    assert result.reboot_required is False
    assert result.current == "24.10.0.1"
    assert result.latest == "24.04.0"
    assert "STABLE" in result.summary
    assert "TrueNAS update available" in result.summary
    assert "upgrade: 23.10.0.1 -> 24.04.0" in result.details
    assert result.apply_hint == "TrueNAS GUI → System Settings → Update"


def test_truenas_current_clean() -> None:
    """CURRENT status → clean, no update, no error."""
    result = run_manual_update_check("truenas_scale", _truenas_fixture("truenas_current.txt"), "nas-01")

    assert not result.error
    assert result.update_available is False
    assert result.reboot_required is False
    assert result.current == "24.10.0.1"
    assert "up to date" in result.summary


def test_truenas_json_string_version_is_normalized() -> None:
    """Some middleware builds serialize system.version as a JSON string."""
    stdout = (
        '"TrueNAS-SCALE-24.10.2.2"\n'
        "@@MANUAL_UPDATE_SEPARATOR@@\n"
        '{"status":"UNAVAILABLE","changes":[]}\n'
    )
    ex = ScriptedManualExecutor(script={"midclt": [_ok(stdout=stdout)]})
    result = run_manual_update_check("truenas_scale", ex, "nas-01")
    assert not result.error
    assert result.current == "24.10.2.2"


def test_truenas_unavailable_clean() -> None:
    """UNAVAILABLE is a known no-update status → clean."""
    result = run_manual_update_check(
        "truenas_scale", _truenas_fixture("truenas_unavailable.txt"), "nas-01"
    )

    assert not result.error
    assert result.update_available is False
    assert "up to date" in result.summary


def test_truenas_reboot_required() -> None:
    """REBOOT_REQUIRED status → reboot flagged, not an available update."""
    result = run_manual_update_check("truenas_scale", _truenas_fixture("truenas_reboot.txt"), "nas-01")

    assert not result.error
    assert result.update_available is False
    assert result.reboot_required is True
    assert "reboot" in result.summary.lower()
    assert "upgrade: 24.04.0 -> 24.10.0.1" in result.details
    assert "Reboot to complete the update." in result.details


def test_truenas_malformed_json() -> None:
    """Malformed JSON payload → error, not unreachable."""
    result = run_manual_update_check(
        "truenas_scale", _truenas_fixture("truenas_malformed.txt"), "nas-01"
    )

    assert result.error
    assert "Malformed" in result.error
    assert result.unreachable is False
    assert result.update_available is False


def test_truenas_unknown_status() -> None:
    """Unknown status string → fail closed with an error."""
    result = run_manual_update_check(
        "truenas_scale", _truenas_fixture("truenas_unknown_status.txt"), "nas-01"
    )

    assert result.error
    assert "Unknown TrueNAS update status" in result.error
    assert result.unreachable is False
    assert result.update_available is False


def test_truenas_component_details_dict_form() -> None:
    """changes with dict-form old/new and a notes field are normalized."""
    result = run_manual_update_check(
        "truenas_scale", _truenas_fixture("truenas_components_dict.txt"), "nas-01"
    )

    assert not result.error
    assert result.update_available is True
    assert result.latest == "24.04.3"
    assert result.current == "24.04.2.1"
    assert "upgrade: 24.04.2.1 -> 24.04.3" in result.details
    assert "Release notes: https://www.truenas.com/docs/changelog/" in result.details


def test_truenas_command_failure_not_unreachable() -> None:
    """midclt failing (rc=1) is a command failure, not an unreachable host."""
    ex = ScriptedManualExecutor(
        script={"midclt": [PrimitiveResult(rc=1, failed=True, stderr="midclt: command not found")]}
    )
    result = run_manual_update_check("truenas_scale", ex, "nas-01")

    assert result.error
    assert result.unreachable is False
    assert result.update_available is False


# ---------------------------------------------------------------------------
# OPNsense parser
# ---------------------------------------------------------------------------


def test_opnsense_available_rc2() -> None:
    """rc=2 is the opnsense-update 'update available' convention."""
    ex = _opnsense_fixture("opnsense_available.txt", rc=2, failed=True)
    result = run_manual_update_check("opnsense", ex, "fw-01")

    assert not result.error
    assert result.unreachable is False
    assert result.update_available is True
    assert result.reboot_required is False
    assert result.current == "24.1.10"
    assert result.latest == "24.7.11"
    assert "24.7.11" in result.summary
    assert "Components: base system, kernel, packages" in result.details
    assert result.apply_hint == "OPNsense GUI → System → Firmware → Status"


def test_opnsense_available_classic_rc0_message() -> None:
    """Classic message-driven variant: rc=0 with 'A newer version is available'."""
    ex = _opnsense_fixture("opnsense_available_classic.txt", rc=0)
    result = run_manual_update_check("opnsense", ex, "fw-01")

    assert not result.error
    assert result.update_available is True
    assert result.current == "24.1.10_3"
    assert result.latest == ""
    assert "24.1.10_3" in result.summary


def test_opnsense_rc2_without_message() -> None:
    """rc=2 alone (silent check output) still means update available."""
    ex = _opnsense_fixture("opnsense_rc2.txt", rc=2)
    result = run_manual_update_check("opnsense", ex, "fw-01")

    assert not result.error
    assert result.update_available is True
    assert result.current == "24.1.10"
    assert "update available" in result.summary


def test_opnsense_no_update() -> None:
    """'Currently up to date.' with rc=0 → clean no-update state."""
    ex = _opnsense_fixture("opnsense_noupdate.txt", rc=0)
    result = run_manual_update_check("opnsense", ex, "fw-01")

    assert not result.error
    assert result.update_available is False
    assert result.unreachable is False
    assert "up to date" in result.summary
    assert result.current == "24.1.10"


def test_opnsense_nothing_to_do() -> None:
    """'Nothing to do.' with rc=0 → clean no-update state."""
    ex = _opnsense_fixture("opnsense_nothing.txt", rc=0)
    result = run_manual_update_check("opnsense", ex, "fw-01")

    assert not result.error
    assert result.update_available is False
    assert "up to date" in result.summary


def test_opnsense_check_error() -> None:
    """rc=1 with failure output → error, not unreachable, not available."""
    ex = _opnsense_fixture("opnsense_error.txt", rc=1, failed=True)
    result = run_manual_update_check("opnsense", ex, "fw-01")

    assert result.error
    assert "check failed" in result.error
    assert result.unreachable is False
    assert result.update_available is False


def test_opnsense_unknown_output_fails_closed() -> None:
    """rc=0 with unrecognized output → error rather than guessing."""
    ex = _opnsense_fixture("opnsense_unknown.txt", rc=0)
    result = run_manual_update_check("opnsense", ex, "fw-01")

    assert result.error
    assert "Unrecognized opnsense-update -c output" in result.error
    assert result.unreachable is False
    assert result.update_available is False


# ---------------------------------------------------------------------------
# Registry and unknown adapter
# ---------------------------------------------------------------------------


def test_registry_ships_both_adapters() -> None:
    assert sorted(MANUAL_UPDATE_REGISTRY.names()) == ["opnsense", "truenas_scale"]
    assert "truenas_scale" in MANUAL_UPDATE_REGISTRY
    assert "opnsense" in MANUAL_UPDATE_REGISTRY
    assert len(MANUAL_UPDATE_REGISTRY) == 2


def test_unknown_adapter_no_host_contact() -> None:
    """Unknown adapter name → error result without any executor call."""
    ex = ScriptedManualExecutor()
    result = run_manual_update_check("no-such-adapter", ex, "host-01")

    assert result.error
    assert "Unknown manual update adapter" in result.error
    assert result.unreachable is False
    assert result.adapter == "no-such-adapter"
    assert result.display_name == "no-such-adapter"
    assert ex.commands == []


def test_registry_get_raises_for_unknown() -> None:
    with pytest.raises(UnknownManualUpdateAdapterError):
        MANUAL_UPDATE_REGISTRY.get("nope")


def test_registry_register_and_run_custom_adapter() -> None:
    class DummyAdapter:
        name = "dummy"
        display_name = "Dummy Appliance"
        apply_hint = "Dummy GUI"

        def validate(self) -> None:
            return None

        def check(self, executor: Any, host: str) -> ManualUpdateResult:
            return ManualUpdateResult(
                host=host,
                display_name=self.display_name,
                adapter=self.name,
                summary="dummy says hi",
            )

    registry = ManualUpdateRegistry()
    registry.register(DummyAdapter())
    assert registry.names() == ["dummy"]

    ex = ScriptedManualExecutor()
    result = run_manual_update_check("dummy", ex, "appliance-1", registry=registry)

    assert not result.error
    assert result.summary == "dummy says hi"
    assert result.host == "appliance-1"


# ---------------------------------------------------------------------------
# Validation before host contact
# ---------------------------------------------------------------------------


def test_validate_rejects_forbidden_tokens() -> None:
    """A command carrying an install-ish token must fail validation."""
    adapter = TrueNASScaleAdapter()
    adapter.check_command = "apt-get update && midclt call system.version"
    with pytest.raises(ManualUpdateAdapterError):
        adapter.validate()


def test_validate_rejects_bare_opnsense_update() -> None:
    """opnsense-update must only ever appear as 'opnsense-update -c'."""
    adapter = OpnsenseAdapter()
    adapter.check_command = "opnsense-update && printf x"
    with pytest.raises(ManualUpdateAdapterError):
        adapter.validate()


def test_validation_failure_no_host_contact() -> None:
    """Validation failures surface as error results before any executor call."""

    class BrokenAdapter(BaseManualUpdateAdapter):
        name = "broken"
        display_name = "Broken"
        apply_hint = "nowhere"
        forbidden_tokens = ("upgrade",)  # install-ish token must trip validation
        check_command = "upgrade --everything && printf '\n@@MANUAL_UPDATE_SEPARATOR@@\n'"

    registry = ManualUpdateRegistry()
    registry.register(BrokenAdapter())
    ex = ScriptedManualExecutor()
    result = run_manual_update_check("broken", ex, "host-01", registry=registry)

    assert result.error
    assert "forbidden token" in result.error
    assert result.unreachable is False
    assert ex.commands == []


# ---------------------------------------------------------------------------
# Command safety (exact strings, no install/apply)
# ---------------------------------------------------------------------------

_OPNSENSE_BARE_RE = re.compile(r"opnsense-update(?!\s-c\b)")


def test_truenas_command_safety() -> None:
    cmd = TrueNASScaleAdapter().check_command
    assert "midclt call system.version" in cmd
    assert "midclt call update.check_available" in cmd
    for token in ("apt", "jq", "updater", "apply", "upgrade"):
        assert token not in cmd


def test_opnsense_command_safety() -> None:
    cmd = OpnsenseAdapter().check_command
    assert "opnsense-version 2>&1" in cmd
    assert "opnsense-update -c 2>&1" in cmd
    # every opnsense-update occurrence must be the -c check, never bare
    assert _OPNSENSE_BARE_RE.findall(cmd) == []
    for token in ("apt", "apply", "upgrade", "pkg"):
        assert token not in cmd


# ---------------------------------------------------------------------------
# Execution contract: changed_when=False, single call
# ---------------------------------------------------------------------------


def test_truenas_uses_changed_when_false() -> None:
    ex = _truenas_fixture("truenas_available.txt")
    run_manual_update_check("truenas_scale", ex, "nas-01")

    assert len(ex.commands) == 1
    assert len(ex.opts) == 1
    assert ex.opts[0].get("changed_when") is False


def test_opnsense_uses_changed_when_false() -> None:
    ex = _opnsense_fixture("opnsense_noupdate.txt", rc=0)
    run_manual_update_check("opnsense", ex, "fw-01")

    assert len(ex.commands) == 1
    assert ex.opts[0].get("changed_when") is False


# ---------------------------------------------------------------------------
# Unreachable normalization
# ---------------------------------------------------------------------------


def test_unreachable_via_result_flag() -> None:
    ex = ScriptedManualExecutor(
        script={"midclt": [_unreachable_ok(stderr="Connection refused")]}
    )
    result = run_manual_update_check("truenas_scale", ex, "nas-01")

    assert result.unreachable is True
    assert result.error
    assert result.update_available is False


def test_unreachable_via_raised_type() -> None:
    ex = ScriptedManualExecutor(raises={"midclt": UnreachableHostError("host did not answer")})
    result = run_manual_update_check("truenas_scale", ex, "nas-01")

    assert result.unreachable is True
    assert "Host unreachable" in result.error
    assert result.update_available is False


def test_unreachable_via_ssh_text() -> None:
    """Recognized SSH error text is unreachable even without the ansible flag."""
    ex = ScriptedManualExecutor(script={"midclt": [_ok(stderr="No route to host")]})
    result = run_manual_update_check("truenas_scale", ex, "nas-01")

    assert result.unreachable is True
    assert result.error
    assert result.update_available is False


def test_unreachable_skips_parser() -> None:
    """Unreachable wins even when stdout looks parseable: no parser invocation."""
    ex = ScriptedManualExecutor(
        script={"midclt": [_unreachable_ok(stdout="TrueNAS-SCALE-24.10.0.1\n@@MANUAL_UPDATE_SEPARATOR@@\n{}")]}
    )
    result = run_manual_update_check("truenas_scale", ex, "nas-01")

    assert result.unreachable is True
    assert result.error
    assert result.summary == ""
    assert result.current == ""


def test_opnsense_unreachable_via_flag() -> None:
    ex = ScriptedManualExecutor(script={"opnsense-update": [_unreachable_ok(stderr="Connection timed out")]})
    result = run_manual_update_check("opnsense", ex, "fw-01")

    assert result.unreachable is True
    assert result.error
    assert result.update_available is False


# ---------------------------------------------------------------------------
# Result is always full-shaped
# ---------------------------------------------------------------------------


def test_result_full_shape_on_success() -> None:
    result = run_manual_update_check(
        "truenas_scale", _truenas_fixture("truenas_available.txt"), "nas-01"
    )

    assert result.host == "nas-01"
    assert result.display_name == "TrueNAS SCALE"
    assert result.adapter == "truenas_scale"
    assert isinstance(result.details, list)
    assert result.details
    assert result.apply_hint


def test_result_full_shape_on_unknown_adapter() -> None:
    result = run_manual_update_check("nope", ScriptedManualExecutor(), "host-01")

    assert result.host == "host-01"
    assert result.display_name == "nope"
    assert result.adapter == "nope"
    assert result.current == ""
    assert result.latest == ""
    assert result.update_available is False
    assert result.reboot_required is False
    assert result.summary == ""
    assert result.details == []
    assert result.apply_hint == ""
    assert result.unreachable is False
    assert result.error


def test_result_full_shape_on_unreachable() -> None:
    ex = ScriptedManualExecutor(script={"midclt": [_unreachable_ok(stderr="Connection refused")]})
    result = run_manual_update_check("truenas_scale", ex, "nas-01")

    assert result.host == "nas-01"
    assert result.adapter == "truenas_scale"
    assert result.display_name == "TrueNAS SCALE"
    assert result.unreachable is True
    assert result.error
    assert result.apply_hint == "TrueNAS GUI → System Settings → Update"
