"""Tests for proxmox_fleet.models.settings.GlobalSettings."""

from proxmox_fleet.models.settings import GlobalSettings, PveClusterCreds


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


def test_new_timeout_fields_have_correct_defaults():
    s = GlobalSettings()
    assert s.apt_proxy_check_timeout == 30.0
    assert s.node_reboot_port_wait_timeout == 300.0
    assert s.snapshot_retries == 3
    assert s.snapshot_retry_delay == 15.0
    assert s.snapshot_timeout == 600
    assert s.snapshot_api_timeout == 30
    assert s.notifier_retries == 15
    assert s.deadmans_retries == 5
    assert s.node_apt_retries == 5
    assert s.node_apt_retry_delay == 30.0


def test_canary_hosts_entries_coerced_to_str():
    s = GlobalSettings.model_validate({"canary_hosts": [101, "media-vm"]})
    assert s.canary_hosts == ["101", "media-vm"]


def test_canary_defaults():
    s = GlobalSettings()
    assert s.canary_hosts == []
    assert s.canary_soak_minutes == 0.0


# --- Task 1: qualified-id (cluster/vmid) settings validation ----------------------


def test_exclude_list_entries_coerced_to_str():
    s = GlobalSettings.model_validate({"exclude_list": [103, "110"]})
    assert s.exclude_list == ["103", "110"]


def test_id_lists_accept_qualified_cluster_tokens():
    s = GlobalSettings.model_validate({
        "exclude_list": [103, "alpha/110"],
        "os_update_exclude_list": ["alpha/120"],
        "app_update_exclude_list": ["beta/130"],
        "snapshot_exclude_list": ["alpha/117"],
        "os_only_lxc_list": ["beta/140"],
    })
    assert s.exclude_list == ["103", "alpha/110"]
    assert s.os_update_exclude_list == ["alpha/120"]
    assert s.app_update_exclude_list == ["beta/130"]
    assert s.snapshot_exclude_list == ["alpha/117"]
    assert s.os_only_lxc_list == ["beta/140"]


def test_id_lists_default_empty():
    s = GlobalSettings()
    assert s.exclude_list == []
    assert s.os_update_exclude_list == []
    assert s.app_update_exclude_list == []
    assert s.snapshot_exclude_list == []
    assert s.os_only_lxc_list == []


def test_id_lists_load_mixed_int_and_qualified_from_yaml(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text(
        "exclude_list:\n  - 103\n  - \"alpha/110\"\n"
        "os_only_lxc_list:\n  - \"beta/140\"\n"
    )
    s = GlobalSettings.load(f)
    assert s.exclude_list == ["103", "alpha/110"]
    assert s.os_only_lxc_list == ["beta/140"]


# --- Task 3: per-cluster PVE API credentials ----------------------------------------


def test_pve_cluster_creds_defaults_empty():
    c = PveClusterCreds()
    assert c.pve_api_user == ""
    assert c.pve_api_token_id == ""
    assert c.pve_api_token_secret == ""


def test_pve_clusters_defaults_empty_dict():
    s = GlobalSettings()
    assert s.pve_clusters == {}


def test_pve_clusters_parses_nested_creds():
    s = GlobalSettings.model_validate({
        "pve_clusters": {
            "alpha": {
                "pve_api_user": "root@pam",
                "pve_api_token_id": "ansible",
                "pve_api_token_secret": "alpha-secret",
            },
            "beta": {
                "pve_api_token_secret": "beta-secret",
            },
        }
    })
    assert s.pve_clusters["alpha"].pve_api_user == "root@pam"
    assert s.pve_clusters["alpha"].pve_api_token_id == "ansible"
    assert s.pve_clusters["alpha"].pve_api_token_secret == "alpha-secret"
    # beta only overrides one field — the rest default to empty (fallback is
    # api_creds()'s job, not the model's).
    assert s.pve_clusters["beta"].pve_api_user == ""
    assert s.pve_clusters["beta"].pve_api_token_secret == "beta-secret"


def test_pve_clusters_loads_from_yaml(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text(
        "pve_api_user: root@pam\n"
        "pve_api_token_id: ansible\n"
        "pve_api_token_secret: global-secret\n"
        "pve_clusters:\n"
        "  beta:\n"
        "    pve_api_token_secret: beta-secret\n"
    )
    s = GlobalSettings.load(f)
    assert s.pve_api_token_secret == "global-secret"
    assert s.pve_clusters["beta"].pve_api_token_secret == "beta-secret"
    assert s.pve_clusters["beta"].pve_api_user == ""
