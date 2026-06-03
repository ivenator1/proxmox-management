"""Tests for proxmox_fleet.models.settings.GlobalSettings."""
import pytest

from proxmox_fleet.models.settings import GlobalSettings


def test_all_defaults():
    s = GlobalSettings()
    assert s.kuma_url == ""
    assert s.kuma_health_check_retries == 5
    assert s.kuma_health_check_delay == 30.0
    assert s.custom_dry_run is False
    assert s.fleet_dry_run is False
    assert s.custom_allow_reboot is True
    assert s.force_window is False
    assert s.configs_dir == "configs"
    assert s.host_vars_dir == "host_vars"


def test_load_missing_file_returns_defaults(tmp_path):
    s = GlobalSettings.load(tmp_path / "nonexistent.yml")
    assert s.kuma_url == ""
    assert s.kuma_health_check_retries == 5


def test_load_from_yaml(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text(
        "kuma_url: http://kuma:3001\n"
        "kuma_health_check_retries: 3\n"
        "custom_dry_run: true\n"
        "fleet_dry_run: false\n"
    )
    s = GlobalSettings.load(f)
    assert s.kuma_url == "http://kuma:3001"
    assert s.kuma_health_check_retries == 3
    assert s.custom_dry_run is True
    assert s.fleet_dry_run is False


def test_extra_fields_allowed(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text("discord_webhook: https://discord.example.com\n")
    s = GlobalSettings.load(f)
    assert s.kuma_url == ""  # defaults still work with extra fields


def test_load_empty_yaml(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text("")
    s = GlobalSettings.load(f)
    assert s.configs_dir == "configs"


def test_load_configs_dir_override(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text("configs_dir: /opt/fleet/configs\n")
    s = GlobalSettings.load(f)
    assert s.configs_dir == "/opt/fleet/configs"


def test_node_field_defaults():
    s = GlobalSettings()
    assert s.manager_lxc_id == ""
    assert s.apt_proxy_ip == ""
    assert s.apt_proxy_port == 3142
    assert s.node_dry_run is False
    assert s.node_auto_reboot is True


def test_node_fields_load_from_yaml(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text(
        "manager_lxc_id: '121'\n"
        "apt_proxy_ip: 10.0.0.5\n"
        "apt_proxy_port: 3143\n"
        "node_dry_run: true\n"
        "node_auto_reboot: false\n"
    )
    s = GlobalSettings.load(f)
    assert s.manager_lxc_id == "121"
    assert s.apt_proxy_ip == "10.0.0.5"
    assert s.apt_proxy_port == 3143
    assert s.node_dry_run is True
    assert s.node_auto_reboot is False


def test_integer_kuma_map_keys_are_coerced_to_str():
    """vars.yml naturally writes integer vmids as keys; they must not crash load."""
    s = GlobalSettings.model_validate({
        "lxc_kuma_map": {101: 5},
        "vm_kuma_map": {200: 9},
        "remote_kuma_map": {"web": 3},
    })
    assert s.lxc_kuma_map == {"101": 5}
    assert s.lxc_kuma_map.get("101") == 5
    assert s.vm_kuma_map == {"200": 9}
    assert s.remote_kuma_map == {"web": 3}


def test_integer_kuma_map_keys_load_from_yaml(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text("lxc_kuma_map:\n  101: 5\n  102: 6\n")
    s = GlobalSettings.load(f)
    assert s.lxc_kuma_map == {"101": 5, "102": 6}
