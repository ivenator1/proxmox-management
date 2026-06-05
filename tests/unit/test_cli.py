"""Tests for proxmox_fleet.cli — argument parsing and settings propagation."""
from __future__ import annotations

import pytest
from unittest.mock import patch

from proxmox_fleet import cli
from proxmox_fleet.models.settings import GlobalSettings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(argv: list, *, run_fleet_return: int = 0):
    """Call cli.main() with synthetic argv.

    Returns (exit_code, settings_passed, check, extra_vars).
    """
    captured = {}

    def _fake_run_fleet(*, settings, inventory_path, check, extra_vars):
        captured["settings"] = settings
        captured["check"] = check
        captured["inventory_path"] = inventory_path
        captured["extra_vars"] = extra_vars or {}
        return run_fleet_return

    with (
        patch("proxmox_fleet.driver.run_fleet", side_effect=_fake_run_fleet),
        patch("proxmox_fleet.models.settings.GlobalSettings.load", return_value=GlobalSettings()),
    ):
        rc = cli.main(argv)

    return rc, captured["settings"], captured["check"], captured.get("extra_vars", {})


# ---------------------------------------------------------------------------
# _parse_extra_vars
# ---------------------------------------------------------------------------

def test_parse_extra_vars_empty_list():
    assert cli._parse_extra_vars([]) == {}


def test_parse_extra_vars_single_pair():
    assert cli._parse_extra_vars(["key=value"]) == {"key": "value"}


def test_parse_extra_vars_multiple_pairs():
    result = cli._parse_extra_vars(["a=1", "b=2"])
    assert result == {"a": "1", "b": "2"}


def test_parse_extra_vars_value_with_equals():
    assert cli._parse_extra_vars(["key=a=b"]) == {"key": "a=b"}


def test_parse_extra_vars_key_trimmed():
    assert cli._parse_extra_vars([" key =val"]) == {"key": "val"}


def test_parse_extra_vars_no_equals_raises_systemexit():
    with pytest.raises(SystemExit):
        cli._parse_extra_vars(["noequals"])


# ---------------------------------------------------------------------------
# _is_true
# ---------------------------------------------------------------------------

def test_is_true_lowercase_true():
    assert cli._is_true("true") is True


def test_is_true_titlecase_true():
    assert cli._is_true("True") is True


def test_is_true_one():
    assert cli._is_true("1") is True


def test_is_true_yes():
    assert cli._is_true("yes") is True


def test_is_true_uppercase_yes():
    assert cli._is_true("YES") is True


def test_is_true_false_string():
    assert cli._is_true("false") is False


def test_is_true_zero():
    assert cli._is_true("0") is False


def test_is_true_no():
    assert cli._is_true("no") is False


def test_is_true_empty():
    assert cli._is_true("") is False


def test_is_true_unrecognised():
    assert cli._is_true("maybe") is False


# ---------------------------------------------------------------------------
# cli.main() — settings propagation
# ---------------------------------------------------------------------------

def test_defaults_all_false():
    _, settings, check, _ = _run([])
    assert settings.fleet_dry_run is False
    assert settings.force_notify is False
    assert settings.lxc_verbose is False
    assert settings.force_window is False
    assert check is False


def test_check_flag_sets_check_only():
    _, settings, check, _ = _run(["--check"])
    assert check is True
    assert settings.fleet_dry_run is False


def test_fleet_dry_run_extravars_propagates():
    _, settings, _, _ = _run(["-e", "fleet_dry_run=true"])
    assert settings.fleet_dry_run is True


def test_lxc_verbose_extravars_propagates():
    _, settings, _, _ = _run(["-e", "lxc_verbose=true"])
    assert settings.lxc_verbose is True


def test_force_notify_extravars_propagates():
    _, settings, _, _ = _run(["-e", "force_notify=true"])
    assert settings.force_notify is True


def test_force_window_extravars_propagates():
    _, settings, _, _ = _run(["-e", "force_window=true"])
    assert settings.force_window is True


def test_unknown_extravars_pass_through():
    _, _, _, ev = _run(["-e", "custom_allow_reboot=false"])
    assert ev.get("custom_allow_reboot") == "false"


def test_bad_extravars_raises_systemexit():
    with pytest.raises(SystemExit):
        _run(["-e", "noequals"])


def test_inventory_forwarded():
    captured = {}

    def _fake(*, settings, inventory_path, check, extra_vars):
        captured["inventory_path"] = inventory_path
        return 0

    with (
        patch("proxmox_fleet.driver.run_fleet", side_effect=_fake),
        patch("proxmox_fleet.models.settings.GlobalSettings.load", return_value=GlobalSettings()),
    ):
        cli.main(["--inventory", "custom.ini"])

    assert captured["inventory_path"] == "custom.ini"


def test_vars_file_forwarded():
    with (
        patch("proxmox_fleet.driver.run_fleet", return_value=0),
        patch("proxmox_fleet.models.settings.GlobalSettings.load", return_value=GlobalSettings()) as mock_load,
    ):
        cli.main(["--vars-file", "custom-vars.yml"])

    mock_load.assert_called_once_with("custom-vars.yml")


def test_exit_code_forwarded():
    rc, _, _, _ = _run([], run_fleet_return=1)
    assert rc == 1
