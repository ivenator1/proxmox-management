"""Tests for proxmox_fleet.lock — the fleet-wide run lock.

flock conflicts are per open-file-description, so two acquire attempts in one
process conflict — no forking needed. CLI integration is tested via the same
patching pattern as test_cli.py.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from proxmox_fleet import cli
from proxmox_fleet.lock import FleetLockHeld, acquire_run_lock, probe_lock
from proxmox_fleet.models.settings import GlobalSettings


# --- acquire_run_lock ---------------------------------------------------------

def test_acquire_and_release(tmp_path):
    with acquire_run_lock(tmp_path):
        pass
    # Reacquire after release works.
    with acquire_run_lock(tmp_path):
        pass


def test_second_acquire_raises_with_holder_info(tmp_path):
    with ExitStack() as stack:
        stack.enter_context(acquire_run_lock(tmp_path, argv=["fleet-update", "--check"]))
        with pytest.raises(FleetLockHeld) as exc:
            stack.enter_context(acquire_run_lock(tmp_path))
        assert isinstance(exc.value.pid, int)
        assert str(exc.value.started).endswith("Z")
        assert exc.value.info["argv"] == ["fleet-update", "--check"]
        assert "pid" in str(exc.value)


def test_lock_file_carries_payload_while_held(tmp_path):
    with acquire_run_lock(tmp_path, argv=["x"]):
        data = json.loads((tmp_path / ".fleet-update.lock").read_text())
        assert data["argv"] == ["x"]
        assert data["pid"] > 0
    # Payload blanked on release so stale holders are never reported.
    assert (tmp_path / ".fleet-update.lock").read_text() == ""


def test_lock_creates_history_dir(tmp_path):
    target = tmp_path / "deep" / "dir"
    with acquire_run_lock(target):
        assert (target / ".fleet-update.lock").exists()


def test_release_on_exception(tmp_path):
    with pytest.raises(RuntimeError):
        with acquire_run_lock(tmp_path):
            raise RuntimeError("boom")
    with acquire_run_lock(tmp_path):  # must not raise
        pass


# --- probe_lock -----------------------------------------------------------------

def test_probe_free(tmp_path):
    assert probe_lock(tmp_path) is None


def test_probe_held_returns_holder(tmp_path):
    with acquire_run_lock(tmp_path, argv=["fleet-update"]):
        info = probe_lock(tmp_path)
    assert info is not None
    assert info["argv"] == ["fleet-update"]


def test_probe_does_not_take_the_lock(tmp_path):
    assert probe_lock(tmp_path) is None
    with acquire_run_lock(tmp_path):  # probe must have released
        pass


# --- run_locked / CLI integration ------------------------------------------------

def _settings(tmp_path) -> GlobalSettings:
    return GlobalSettings(fleet_history_dir=str(tmp_path))


def test_run_locked_executes_fn(tmp_path):
    assert cli.run_locked(_settings(tmp_path), lambda: 7) == 7


def test_run_locked_rc1_when_held(tmp_path, capsys):
    with acquire_run_lock(tmp_path):
        rc = cli.run_locked(_settings(tmp_path), lambda: 0)
    assert rc == 1
    assert "refusing to overlap" in capsys.readouterr().err


def test_cli_fleet_run_refuses_overlap(tmp_path, capsys):
    with (
        acquire_run_lock(tmp_path),
        patch("proxmox_fleet.driver.run_fleet", return_value=0) as mock_fleet,
        patch("proxmox_fleet.models.settings.GlobalSettings.load",
              return_value=_settings(tmp_path)),
    ):
        rc = cli.main(["--check"])

    assert rc == 1
    mock_fleet.assert_not_called()
    assert "another fleet run is active" in capsys.readouterr().err


def test_cli_scan_refuses_overlap(tmp_path, capsys):
    with (
        acquire_run_lock(tmp_path),
        patch("proxmox_fleet.scan.run_fleet_scan", return_value=0) as mock_scan,
        patch("proxmox_fleet.models.settings.GlobalSettings.load",
              return_value=_settings(tmp_path)),
    ):
        rc = cli.main(["--scan"])

    assert rc == 1
    mock_scan.assert_not_called()


def test_cli_run_acquires_and_releases(tmp_path):
    with (
        patch("proxmox_fleet.driver.run_fleet", return_value=0),
        patch("proxmox_fleet.models.settings.GlobalSettings.load",
              return_value=_settings(tmp_path)),
    ):
        assert cli.main([]) == 0
    assert probe_lock(tmp_path) is None     # released after the run


def test_cli_history_works_while_lock_held(tmp_path, capsys):
    from proxmox_fleet.history import write_history
    from proxmox_fleet.models.state import FleetState

    write_history(FleetState.from_raw({}), history_dir=tmp_path, keep=0,
                  timestamp="20260101T000000000000Z")

    with (
        acquire_run_lock(tmp_path),
        patch("proxmox_fleet.models.settings.GlobalSettings.load",
              return_value=_settings(tmp_path)),
    ):
        rc = cli.main(["--history"])

    assert rc == 0
    assert "20260101T000000000000Z" in capsys.readouterr().out
