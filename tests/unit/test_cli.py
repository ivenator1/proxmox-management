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


# ---------------------------------------------------------------------------
# cli.main() — --history / --history-show (early exit, no fleet run)
# ---------------------------------------------------------------------------

def _write_history(tmp_path, *, timestamp, changed=False, failed=False, briefing=None):
    from proxmox_fleet.history import write_history
    from proxmox_fleet.models.state import FleetState

    state = FleetState.from_raw({"fleet_changed": changed, "fleet_failed": failed})
    write_history(state, history_dir=tmp_path, keep=0, timestamp=timestamp,
                  briefing=briefing)


def _settings_with_history(tmp_path) -> GlobalSettings:
    return GlobalSettings(fleet_history_dir=str(tmp_path))


def test_history_lists_runs_without_running_fleet(tmp_path, capsys):
    _write_history(tmp_path, timestamp="20260101T000000000000Z")
    _write_history(tmp_path, timestamp="20260102T000000000000Z", changed=True)

    with (
        patch("proxmox_fleet.driver.run_fleet") as mock_fleet,
        patch("proxmox_fleet.models.settings.GlobalSettings.load",
              return_value=_settings_with_history(tmp_path)),
    ):
        rc = cli.main(["--history"])

    mock_fleet.assert_not_called()
    assert rc == 0
    out = capsys.readouterr().out
    # Newest first, header row present.
    assert out.index("20260102T000000000000Z") < out.index("20260101T000000000000Z")
    assert "TIMESTAMP" in out and "RESULT" in out


def test_history_limit_respected(tmp_path, capsys):
    for i in range(4):
        _write_history(tmp_path, timestamp=f"2026010{i}T000000000000Z")

    with patch("proxmox_fleet.models.settings.GlobalSettings.load",
               return_value=_settings_with_history(tmp_path)):
        rc = cli.main(["--history", "2"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "20260103T000000000000Z" in out and "20260102T000000000000Z" in out
    assert "20260101T000000000000Z" not in out


def test_history_empty_dir_exits_nonzero(tmp_path, capsys):
    with patch("proxmox_fleet.models.settings.GlobalSettings.load",
               return_value=_settings_with_history(tmp_path)):
        rc = cli.main(["--history"])

    assert rc == 1
    assert "no run history" in capsys.readouterr().out


def test_history_show_prints_briefing(tmp_path, capsys):
    _write_history(tmp_path, timestamp="20260101T000000000000Z",
                   briefing="**Fleet Update Complete**\n- sonarr OK")

    with patch("proxmox_fleet.models.settings.GlobalSettings.load",
               return_value=_settings_with_history(tmp_path)):
        rc = cli.main(["--history-show", "latest"])

    assert rc == 0
    assert "sonarr OK" in capsys.readouterr().out


def test_history_show_by_timestamp(tmp_path, capsys):
    _write_history(tmp_path, timestamp="20260101T000000000000Z", briefing="OLD RUN")
    _write_history(tmp_path, timestamp="20260102T000000000000Z", briefing="NEW RUN")

    with patch("proxmox_fleet.models.settings.GlobalSettings.load",
               return_value=_settings_with_history(tmp_path)):
        rc = cli.main(["--history-show", "20260101T000000000000Z"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "OLD RUN" in out and "NEW RUN" not in out


def test_history_show_falls_back_to_json_without_briefing(tmp_path, capsys):
    _write_history(tmp_path, timestamp="20260101T000000000000Z")  # no briefing key

    with patch("proxmox_fleet.models.settings.GlobalSettings.load",
               return_value=_settings_with_history(tmp_path)):
        rc = cli.main(["--history-show", "latest"])

    assert rc == 0
    assert '"timestamp": "20260101T000000000000Z"' in capsys.readouterr().out


def test_history_show_missing_run_exits_nonzero(tmp_path, capsys):
    with patch("proxmox_fleet.models.settings.GlobalSettings.load",
               return_value=_settings_with_history(tmp_path)):
        rc = cli.main(["--history-show", "latest"])

    assert rc == 1
    assert "no history record" in capsys.readouterr().out


def test_history_uses_vars_file_history_dir(tmp_path, capsys):
    _write_history(tmp_path, timestamp="20260101T000000000000Z")
    vars_file = tmp_path / "vars.yml"
    vars_file.write_text(f"fleet_history_dir: {tmp_path}\n", encoding="utf-8")

    rc = cli.main(["--history", "--vars-file", str(vars_file)])

    assert rc == 0
    assert "20260101T000000000000Z" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _format_history_table
# ---------------------------------------------------------------------------

def test_format_history_table_result_column():
    rows = [
        {"timestamp": "T3", "changed": False, "failed": True,
         "counts": {"lxc": 1, "errors": 2}},
        {"timestamp": "T2", "changed": True, "failed": False, "counts": {}},
        {"timestamp": "T1", "changed": False, "failed": False, "counts": {}},
    ]
    lines = cli._format_history_table(rows).splitlines()
    assert lines[0].startswith("TIMESTAMP")
    assert "failed" in lines[1] and "changed" in lines[2] and "clean" in lines[3]
    # Counts land under their columns; missing counts render as 0.
    assert lines[1].split() == ["T3", "failed", "1", "0", "0", "0", "0", "2", "0"]
    assert lines[3].split() == ["T1", "clean", "0", "0", "0", "0", "0", "0", "0"]
