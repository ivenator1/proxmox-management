"""Tests for fleet-update.py (repo-root wrapper) and the cli.py force_window fix.

fleet-update.py lives outside proxmox_fleet/ so we import it via importlib.
The venv bootstrap (_bootstrap_venv) is a no-op in tests because we're already
running in the right interpreter — we test the argument-parsing and settings-
propagation logic directly by monkeypatching driver.run_fleet.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from proxmox_fleet.models.settings import GlobalSettings

# ---------------------------------------------------------------------------
# Load fleet-update.py as a module (it lives at the repo root, not in a package)
# ---------------------------------------------------------------------------

_WRAPPER_PATH = Path(__file__).resolve().parents[2] / "fleet-update.py"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location("fleet_update_wrapper", _WRAPPER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # The bootstrap runs at module level — stub os.execv so it never fires.
    with patch("os.execv"):
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_wrapper = _load_wrapper()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(argv: list, *, run_fleet_return: int = 0) -> tuple[int, GlobalSettings, dict]:
    """Call wrapper.main() with synthetic argv.

    Returns (exit_code, settings_passed, extravars_passed).
    """
    captured: Dict[str, Any] = {}

    def _fake_run_fleet(*, settings, inventory_path, check, extra_vars, **kwargs):
        captured["settings"] = settings
        captured["check"] = check
        captured["inventory_path"] = inventory_path
        captured["extra_vars"] = extra_vars or {}
        return run_fleet_return

    with (
        patch.object(sys, "argv", ["./fleet-update.py"] + argv),
        patch("proxmox_fleet.driver.run_fleet", side_effect=_fake_run_fleet),
        patch("proxmox_fleet.models.settings.GlobalSettings.load", return_value=GlobalSettings()),
        patch("proxmox_fleet.cli.run_locked", side_effect=lambda settings, fn: fn()),
    ):
        rc = _wrapper.main()

    return rc, captured["settings"], captured.get("extra_vars", {})


# ---------------------------------------------------------------------------
# Tests: defaults
# ---------------------------------------------------------------------------

def test_defaults_pass_through():
    rc, settings, ev = _run([])
    assert rc == 0
    assert settings.fleet_dry_run is False
    assert settings.force_notify is False
    assert settings.lxc_verbose is False
    assert settings.force_window is False


# ---------------------------------------------------------------------------
# Tests: --dry-run / --check alias
# ---------------------------------------------------------------------------

def test_dry_run_flag():
    # settings.fleet_dry_run (plus check=True) is the contract — the driver no
    # longer needs a synthetic "fleet_dry_run" extravar.
    _, settings, ev = _run(["--dry-run"])
    assert settings.fleet_dry_run is True
    assert "fleet_dry_run" not in ev


def test_check_alias():
    _, settings, ev = _run(["--check"])
    assert settings.fleet_dry_run is True
    assert "fleet_dry_run" not in ev


def test_dry_run_passes_check_true_to_driver():
    captured: Dict[str, Any] = {}

    def _fake(*, settings, inventory_path, check, extra_vars, **kwargs):
        captured["check"] = check
        return 0

    with (
        patch.object(sys, "argv", ["./fleet-update.py", "--dry-run"]),
        patch("proxmox_fleet.driver.run_fleet", side_effect=_fake),
        patch("proxmox_fleet.models.settings.GlobalSettings.load", return_value=GlobalSettings()),
        patch("proxmox_fleet.cli.run_locked", side_effect=lambda settings, fn: fn()),
    ):
        _wrapper.main()

    assert captured["check"] is True


# ---------------------------------------------------------------------------
# Tests: --force-notify / --notify alias
# ---------------------------------------------------------------------------

def test_force_notify_flag():
    _, settings, _ = _run(["--force-notify"])
    assert settings.force_notify is True


def test_notify_alias():
    _, settings, _ = _run(["--notify"])
    assert settings.force_notify is True


# ---------------------------------------------------------------------------
# Tests: --verbose
# ---------------------------------------------------------------------------

def test_verbose_flag():
    _, settings, _ = _run(["--verbose"])
    assert settings.lxc_verbose is True


# ---------------------------------------------------------------------------
# Tests: --force-window
# ---------------------------------------------------------------------------

def test_force_window_flag():
    _, settings, _ = _run(["--force-window"])
    assert settings.force_window is True


# ---------------------------------------------------------------------------
# Tests: -e extra-vars passthrough and propagation
# ---------------------------------------------------------------------------

def test_extra_vars_passthrough():
    _, _, ev = _run(["-e", "custom_allow_reboot=false"])
    assert ev["custom_allow_reboot"] == "false"


def test_extra_var_fleet_dry_run_propagates():
    _, settings, _ = _run(["-e", "fleet_dry_run=true"])
    assert settings.fleet_dry_run is True


def test_extra_var_force_notify_propagates():
    _, settings, _ = _run(["-e", "force_notify=true"])
    assert settings.force_notify is True


def test_extra_var_lxc_verbose_propagates():
    _, settings, _ = _run(["-e", "lxc_verbose=true"])
    assert settings.lxc_verbose is True


def test_extra_var_force_window_propagates():
    _, settings, _ = _run(["-e", "force_window=true"])
    assert settings.force_window is True


# ---------------------------------------------------------------------------
# Tests: friendly flags win over conflicting -e values
# ---------------------------------------------------------------------------

def test_friendly_flag_wins_over_extra_var():
    # -e fleet_dry_run=false then --dry-run: friendly flag must win.
    _, settings, ev = _run(["-e", "fleet_dry_run=false", "--dry-run"])
    assert settings.fleet_dry_run is True


# ---------------------------------------------------------------------------
# Tests: -e bad format
# ---------------------------------------------------------------------------

def test_extra_vars_bad_format_exits():
    with (
        patch.object(sys, "argv", ["./fleet-update.py", "-e", "no-equals-sign"]),
        patch("proxmox_fleet.models.settings.GlobalSettings.load", return_value=GlobalSettings()),
    ):
        with pytest.raises(SystemExit):
            _wrapper.main()


# ---------------------------------------------------------------------------
# Tests: --inventory and --vars-file forwarding
# ---------------------------------------------------------------------------

def test_custom_inventory():
    captured: Dict[str, Any] = {}

    def _fake(*, settings, inventory_path, check, extra_vars, **kwargs):
        captured["inventory_path"] = inventory_path
        return 0

    with (
        patch.object(sys, "argv", ["./fleet-update.py", "--inventory", "/tmp/my-hosts.ini"]),
        patch("proxmox_fleet.driver.run_fleet", side_effect=_fake),
        patch("proxmox_fleet.models.settings.GlobalSettings.load", return_value=GlobalSettings()),
        patch("proxmox_fleet.cli.run_locked", side_effect=lambda settings, fn: fn()),
    ):
        _wrapper.main()

    assert captured["inventory_path"] == "/tmp/my-hosts.ini"


def test_custom_vars_file(tmp_path):
    vars_file = tmp_path / "myvars.yml"
    vars_file.write_text("{}\n")

    captured: Dict[str, Any] = {}

    def _fake(*, settings, inventory_path, check, extra_vars, **kwargs):
        captured["ok"] = True
        return 0

    with (
        patch.object(sys, "argv", ["./fleet-update.py", "--vars-file", str(vars_file)]),
        patch("proxmox_fleet.driver.run_fleet", side_effect=_fake),
        patch("proxmox_fleet.cli.run_locked", side_effect=lambda settings, fn: fn()),
    ):
        _wrapper.main()

    assert captured.get("ok")


# ---------------------------------------------------------------------------
# Tests: exit code forwarded from driver
# ---------------------------------------------------------------------------

def test_exit_code_forwarded():
    rc, _, _ = _run([], run_fleet_return=1)
    assert rc == 1


# ---------------------------------------------------------------------------
# Tests: --limit / --phases forwarding
# ---------------------------------------------------------------------------

def test_limit_and_phases_forwarded():
    captured: Dict[str, Any] = {}

    def _fake(*, settings, inventory_path, check, extra_vars, limit, phases):
        captured["limit"] = limit
        captured["phases"] = phases
        return 0

    with (
        patch.object(sys, "argv",
                     ["./fleet-update.py", "--limit", "pve-01,105", "--phases", "lxc"]),
        patch("proxmox_fleet.driver.run_fleet", side_effect=_fake),
        patch("proxmox_fleet.models.settings.GlobalSettings.load", return_value=GlobalSettings()),
        patch("proxmox_fleet.cli.run_locked", side_effect=lambda settings, fn: fn()),
    ):
        _wrapper.main()

    assert captured["limit"] == {"pve-01", "105"}
    assert captured["phases"] == {"lxc"}


def test_limit_and_phases_default_none():
    captured: Dict[str, Any] = {}

    def _fake(*, settings, inventory_path, check, extra_vars, limit, phases):
        captured["limit"] = limit
        captured["phases"] = phases
        return 0

    with (
        patch.object(sys, "argv", ["./fleet-update.py"]),
        patch("proxmox_fleet.driver.run_fleet", side_effect=_fake),
        patch("proxmox_fleet.models.settings.GlobalSettings.load", return_value=GlobalSettings()),
        patch("proxmox_fleet.cli.run_locked", side_effect=lambda settings, fn: fn()),
    ):
        _wrapper.main()

    assert captured["limit"] is None
    assert captured["phases"] is None


# ---------------------------------------------------------------------------
# Tests: --history / --history-show early exit (no fleet run)
# ---------------------------------------------------------------------------

def _write_history(tmp_path, *, timestamp, briefing=None):
    from proxmox_fleet.history import write_history
    from proxmox_fleet.models.state import FleetState

    write_history(FleetState.from_raw({}), history_dir=tmp_path, keep=0,
                  timestamp=timestamp, briefing=briefing)


def test_history_flag_skips_fleet_run(tmp_path, capsys):
    _write_history(tmp_path, timestamp="20260101T000000000000Z")

    with (
        patch.object(sys, "argv", ["./fleet-update.py", "--history"]),
        patch("proxmox_fleet.driver.run_fleet") as mock_fleet,
        patch("proxmox_fleet.models.settings.GlobalSettings.load",
              return_value=GlobalSettings(fleet_history_dir=str(tmp_path))),
    ):
        rc = _wrapper.main()

    mock_fleet.assert_not_called()
    assert rc == 0
    assert "20260101T000000000000Z" in capsys.readouterr().out


def test_history_show_flag_prints_briefing(tmp_path, capsys):
    _write_history(tmp_path, timestamp="20260101T000000000000Z", briefing="THE BRIEFING")

    with (
        patch.object(sys, "argv", ["./fleet-update.py", "--history-show", "latest"]),
        patch("proxmox_fleet.driver.run_fleet") as mock_fleet,
        patch("proxmox_fleet.models.settings.GlobalSettings.load",
              return_value=GlobalSettings(fleet_history_dir=str(tmp_path))),
    ):
        rc = _wrapper.main()

    mock_fleet.assert_not_called()
    assert rc == 0
    assert "THE BRIEFING" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Tests: cli.py force_window propagation fix
# ---------------------------------------------------------------------------

def test_cli_force_window_propagates(tmp_path):
    """cli.main() must copy force_window=true from -e into settings."""
    from proxmox_fleet import cli

    captured: Dict[str, Any] = {}

    def _fake_fleet(*, settings, inventory_path, check, extra_vars, **kwargs):
        captured["force_window"] = settings.force_window
        return 0

    hosts = tmp_path / "hosts.ini"
    hosts.write_text("[proxmox_nodes]\n")

    with (
        patch("proxmox_fleet.driver.run_fleet", side_effect=_fake_fleet),
        patch("proxmox_fleet.cli.run_locked", side_effect=lambda settings, fn: fn()),
    ):
        cli.main(["--inventory", str(hosts), "-e", "force_window=true"])

    assert captured["force_window"] is True


def test_cli_force_window_false_by_default(tmp_path):
    """cli.main() must NOT set force_window unless the flag is passed."""
    from proxmox_fleet import cli

    captured: Dict[str, Any] = {}

    def _fake_fleet(*, settings, inventory_path, check, extra_vars, **kwargs):
        captured["force_window"] = settings.force_window
        return 0

    hosts = tmp_path / "hosts.ini"
    hosts.write_text("[proxmox_nodes]\n")

    with (
        patch("proxmox_fleet.driver.run_fleet", side_effect=_fake_fleet),
        patch("proxmox_fleet.cli.run_locked", side_effect=lambda settings, fn: fn()),
    ):
        cli.main(["--inventory", str(hosts)])

    assert captured["force_window"] is False


# ---------------------------------------------------------------------------
# Tests: --scan early exit (no fleet run)
# ---------------------------------------------------------------------------

def test_scan_flag_skips_fleet_run():
    captured: Dict[str, Any] = {}

    def _fake_scan(*, settings, inventory_path, limit):
        captured["limit"] = limit
        return 0

    with (
        patch.object(sys, "argv", ["./fleet-update.py", "--scan", "--limit", "105"]),
        patch("proxmox_fleet.scan.run_fleet_scan", side_effect=_fake_scan),
        patch("proxmox_fleet.driver.run_fleet") as mock_fleet,
        patch("proxmox_fleet.models.settings.GlobalSettings.load", return_value=GlobalSettings()),
        patch("proxmox_fleet.cli.run_locked", side_effect=lambda settings, fn: fn()),
    ):
        rc = _wrapper.main()

    mock_fleet.assert_not_called()
    assert rc == 0
    assert captured["limit"] == {"105"}
