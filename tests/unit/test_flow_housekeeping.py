"""Behavioural tests for proxmox_fleet.flows.housekeeping.

The flow is driven with the real maintenance policy
(:func:`proxmox_fleet.housekeeping.run_housekeeping`) and the execution-layer
``Guest`` adapter from ``tests.unit.test_housekeeping``, which reclaims real
temporary cache directories and records the requested native actions through a
real SQLite checkpoint.  The assertions are about the observable flow outcome --
which guests are skipped, what a real clean reclaimed, dry-run safety and that a
failed introspection fails loudly -- never about a mocked policy producer or a
forwarded command string.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from proxmox_fleet import housekeeping_import as hi
from proxmox_fleet.flows import housekeeping as flow
from proxmox_fleet.log_plugins import load_log_catalogue
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult
from tests.unit.test_housekeeping import (
    CacheSpec,
    Guest,
    LokiStub,
    _base,
    _cache_dir,
    _compliant,
    _measure,
    _wire,
)

LXC = "120"
NODE = "pve-01"

#: The immutable catalogue the driver loads once per run.
CATALOGUE = load_log_catalogue()


class FlowGuest(Guest):
    """Execution-layer guest adapter with the flow's ``introspect`` transport."""

    def __init__(self, guest_root: Path, spool: Path, *, name: str = "npm") -> None:
        super().__init__(guest_root, spool)
        self.name = name
        self.community_script_id: Optional[str] = None
        self.introspect_result: Optional[PrimitiveResult] = None
        self.introspect_calls = 0

    def introspect(self, lxc_id: str) -> PrimitiveResult:
        self.introspect_calls += 1
        if self.introspect_result is not None:
            return self.introspect_result
        config = f"hostname: {self.name}\nostype: debian\n"
        if self.template:
            config += "template: 1\n"
        status = "status: running" if (self.running and not self.template) else "status: stopped"
        facts = {
            "config_stdout": config,
            "status_stdout": status,
            "pull_rc": 0 if self.community_script_id else 1,
            "script_stdout": (
                f"source ct/{self.community_script_id}.sh"
                if self.community_script_id
                else ""
            ),
        }
        return PrimitiveResult(rc=0, facts=facts)

    def alloy_reconcile(self, **kwargs: Any) -> PrimitiveResult:
        result = super().alloy_reconcile(**kwargs)
        if result.ok and kwargs.get("configure"):
            deployed = Path(self.guest_root) / "etc/alloy/config.alloy"
            deployed.parent.mkdir(parents=True, exist_ok=True)
            deployed.write_text(kwargs["desired_content"])
        return result


def _settings(history: Path, *, url: str = "", **overrides: Any) -> GlobalSettings:
    values = {
        "fleet_history_dir": str(history),
        "housekeeping_enabled": bool(url),
        "housekeeping_loki_url": url,
    }
    values.update(overrides)
    return GlobalSettings.model_validate(values)


@pytest.fixture
def history(tmp_path: Path) -> Path:
    directory = tmp_path / "history"
    directory.mkdir()
    return directory


@pytest.fixture
def guest(tmp_path: Path, monkeypatch) -> FlowGuest:
    root = tmp_path / "guest"
    (root / "data" / "logs").mkdir(parents=True)
    spool = tmp_path / "spool"
    monkeypatch.setattr(hi, "SPOOL_ROOT", str(spool))
    return FlowGuest(root, spool)


def _run(guest: FlowGuest, settings: GlobalSettings, base=None, *, dry_run: bool = False):
    return flow.run_lxc_housekeeping(
        NODE, LXC, guest, settings, dry_run=dry_run, alloy_config=base,
        catalogue=CATALOGUE, log_override=guest.log_override,
    )


# --------------------------------------------------------------------------- #
# Eligibility: stopped and template guests are untouched
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("stopped,template", [(True, False), (False, True)])
def test_stopped_or_template_guest_is_skipped_without_actions(history, guest, stopped, template):
    guest.running = not stopped
    guest.template = template
    settings = _settings(history)

    out = _run(guest, settings, _base("http://127.0.0.1:9"))

    assert out.record is None
    assert out.failed is False
    assert out.changed is False
    assert out.housekeeping_attempted is False
    assert guest.introspect_calls == 1
    # The maintenance policy was never reached, so the guest (and any checkpoint)
    # is left completely alone.
    assert guest.probe_calls == 0
    assert guest.applied == []
    assert not (history / "housekeeping.sqlite3").exists()


# --------------------------------------------------------------------------- #
# Dry run writes nothing anywhere
# --------------------------------------------------------------------------- #


def test_dry_run_creates_no_state_and_reclaims_nothing(history, guest, tmp_path):
    source = Path(guest.guest_root) / "data/logs/access.log.1"
    source.write_bytes(b"old npm access line\n")
    before_source = source.read_bytes()
    yarn = _cache_dir(tmp_path, "yarn")
    before_yarn = _measure(yarn)
    assert before_yarn > 0
    guest.cache_specs["yarn"] = CacheSpec("/usr/local/share/.cache/yarn/v6", yarn, "1.22.19")

    with LokiStub() as stub:
        settings = _settings(history, url=stub.url)
        out = _run(guest, settings, _base(stub.url), dry_run=True)

    assert not (history / "housekeeping.sqlite3").exists()
    assert guest.capture_calls == 0 and guest.snapshot_calls == 0
    assert guest.cache_commands() == []
    assert _measure(yarn) == before_yarn
    assert source.read_bytes() == before_source
    assert out.changed is False
    assert out.record is not None and out.record.housekeeping.status == "Audit"
    assert out.housekeeping_attempted is True


# --------------------------------------------------------------------------- #
# Real reclamation is measured and retained even when delivery blocks
# --------------------------------------------------------------------------- #


def test_real_cache_clean_survives_blocked_archival(history, guest, tmp_path):
    source = Path(guest.guest_root) / "data/logs/access.log.1"
    source.write_bytes(b"old npm access line\n")
    guest.community_script_id = "nginxproxymanager"
    guest.profiles = ["npm"]
    guest.files = [_wire(Path(guest.guest_root), "/data/logs/access.log.1")]
    yarn = _cache_dir(tmp_path, "yarn")
    before = _measure(yarn)
    assert before > 0
    guest.cache_specs["yarn"] = CacheSpec("/usr/local/share/.cache/yarn/v6", yarn, "1.22.19")

    with LokiStub(push_status=503) as stub:
        settings = _settings(history, url=stub.url)
        out = _run(guest, settings, _base(stub.url))

    # The cache reclamation is real and measured even though archival failed.
    assert _measure(yarn) == 0
    assert out.failed is True
    assert out.housekeeping_attempted is True
    assert out.record is not None
    assert out.record.housekeeping.status == "Blocked"
    assert out.record.housekeeping.bytes_reclaimed == before
    assert out.record.housekeeping.bytes_archived == 0
    assert any(w.notifying for w in out.warnings)
    # Unhealthy delivery never deletes source logs.
    assert source.exists()
    assert guest.prune_calls == []


# --------------------------------------------------------------------------- #
# Failed introspection fails loudly instead of silently skipping
# --------------------------------------------------------------------------- #


def test_failed_introspection_fails_loudly(history, guest):
    guest.introspect_result = PrimitiveResult(rc=1, failed=True, stderr="pct config 120 failed")
    settings = _settings(history)

    with pytest.raises(RuntimeError, match="introspect"):
        _run(guest, settings)

    # A controller that cannot be read is an error, never a silent skip.
    assert guest.probe_calls == 0
    assert not (history / "housekeeping.sqlite3").exists()


# --------------------------------------------------------------------------- #
# An already-compliant guest reports nothing
# --------------------------------------------------------------------------- #


def test_compliant_guest_produces_no_record(history, guest):
    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url)
        _compliant(guest, settings, base)
        out = _run(guest, settings, base)

    assert out.record is None
    assert out.changed is False
    assert out.failed is False
    # No community-script identity: the already-installed base journal config
    # is enforced and the sanitized application-coverage finding is reported.
    assert any("unsupported" in w.warning for w in out.warnings)
    assert all(not w.notifying for w in out.warnings)
    # Idle maintenance still ran: absence of a record must not hide recovery.
    assert out.housekeeping_attempted is True


def test_unsupported_identity_housekeeping_enforces_base_journal_config(history, guest):
    """Housekeeping-only preparation is not skipped for an unknown application:
    it replaces stale generated readers with the base journal pipeline, records
    the configuration, and retains the unsupported coverage diagnostic."""
    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url)
        deployed = Path(guest.guest_root) / "etc/alloy/config.alloy"
        deployed.parent.mkdir(parents=True, exist_ok=True)
        deployed.write_text(base.content + "# previously managed file sources\n")
        out = _run(guest, settings, base)

    assert out.failed is False
    assert out.record is not None
    assert out.record.housekeeping.status == "Configured"
    content = deployed.read_text()
    assert content == base.content
    assert 'loki.source.journal "journal"' in content
    assert "loki.source.file" not in content
    assert "fleet_" not in content
    assert guest.log_acl_calls == []
    assert guest.acl_roots == set()
    assert guest.acl_files == set()
    assert guest.alloy_binary is True
    assert guest.alloy_journal is True
    assert guest.alloy_enabled is True
    assert guest.alloy_active is True
    assert guest.alloy_config_sha == base.sha256
    assert any("unsupported" in w.warning for w in out.warnings)
    assert all(not w.notifying for w in out.warnings)
