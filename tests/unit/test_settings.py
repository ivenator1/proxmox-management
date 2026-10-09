"""Tests for proxmox_fleet.models.settings.GlobalSettings."""

import pytest
from pydantic import ValidationError

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
    assert s.lxc_disk_warn_percent == 75
    assert s.lxc_disk_min_free_gb == 10.0
    assert s.configs_dir == "configs"
    assert s.host_vars_dir == "host_vars"
    assert s.alloy_enabled is False
    assert s.alloy_config_path == "configs/guest.alloy"
    assert s.lxc_alloy_exclude_list == []
    assert s.vm_alloy_exclude_list == []


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


def test_alloy_settings_load_and_lxc_ids_are_coerced(tmp_path):
    path = tmp_path / "vars.yml"
    path.write_text(
        "alloy_enabled: true\n"
        "alloy_config_path: /etc/fleet/guest.alloy\n"
        "lxc_alloy_exclude_list: [501, 'beta/502']\n"
        "vm_alloy_exclude_list: [loki-vm]\n",
        encoding="utf-8",
    )
    settings = GlobalSettings.load(path)
    assert settings.alloy_enabled is True
    assert settings.alloy_config_path == "/etc/fleet/guest.alloy"
    assert settings.lxc_alloy_exclude_list == ["501", "beta/502"]
    assert settings.vm_alloy_exclude_list == ["loki-vm"]


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


# --- manual_update settings (scan-only, no -e support) -----------------------


def test_manual_update_settings_defaults():
    s = GlobalSettings()
    assert s.manual_update_notifications is True
    assert s.manual_update_reminder_hours == 24
    assert s.manual_update_forks == 2


def test_manual_update_settings_load_from_yaml(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text(
        "manual_update_notifications: false\n"
        "manual_update_reminder_hours: 48\n"
        "manual_update_forks: 4\n"
    )
    s = GlobalSettings.load(f)
    assert s.manual_update_notifications is False
    assert s.manual_update_reminder_hours == 48
    assert s.manual_update_forks == 4


def test_manual_update_settings_missing_file_keeps_defaults(tmp_path):
    s = GlobalSettings.load(tmp_path / "nonexistent.yml")
    assert s.manual_update_notifications is True
    assert s.manual_update_reminder_hours == 24
    assert s.manual_update_forks == 2


def test_manual_update_settings_accept_missing_keys_in_yaml(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text("kuma_url: http://kuma:3001\n")
    s = GlobalSettings.load(f)
    assert s.manual_update_notifications is True
    assert s.manual_update_reminder_hours == 24
    assert s.manual_update_forks == 2


def test_manual_update_settings_int_types(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text(
        "manual_update_reminder_hours: 12\n"
        "manual_update_forks: 1\n"
    )
    s = GlobalSettings.load(f)
    assert s.manual_update_reminder_hours == 12
    assert s.manual_update_forks == 1


# --- housekeeping settings (step 1) -----------------------------------------


def test_housekeeping_defaults():
    s = GlobalSettings()
    assert s.housekeeping_enabled is False
    assert s.housekeeping_loki_url == ""
    assert s.housekeeping_local_retention_hours == 48
    assert s.housekeeping_journal_max_mb == 256
    assert s.housekeeping_journal_keep_free_mb == 512
    assert s.housekeeping_cache_interval_hours == 24
    assert s.housekeeping_backfill_budget_mb == 1024
    assert s.lxc_housekeeping_exclude_list == []


@pytest.mark.parametrize(
    "field",
    [
        "housekeeping_local_retention_hours",
        "housekeeping_journal_max_mb",
        "housekeeping_journal_keep_free_mb",
        "housekeeping_cache_interval_hours",
        "housekeeping_backfill_budget_mb",
    ],
)
@pytest.mark.parametrize("bad", [0, -1])
def test_housekeeping_positive_int_fields_reject_nonpositive(field, bad):
    with pytest.raises(ValidationError):
        GlobalSettings.model_validate({field: bad})


@pytest.mark.parametrize(
    "url",
    [
        "http://10.10.10.39:3100",
        "https://loki.example.com:3100",
        "https://loki.example.com/loki",
        "http://loki",
    ],
)
def test_housekeeping_loki_url_accepts_absolute_http_bases(url):
    s = GlobalSettings.model_validate({"housekeeping_loki_url": url})
    assert s.housekeeping_loki_url == url
    assert s.require_loki_url() == url


@pytest.mark.parametrize(
    "url",
    [
        "loki:3100",  # scheme is "loki", not http(s)
        "10.10.10.39:3100",  # no scheme at all
        "//loki:3100",  # protocol-relative, no scheme
        "ftp://loki:3100",  # wrong scheme
        "http://",  # no host
        "http://user:pass@loki:3100",  # credentials must never live here
        "http://loki:3100?tenant=x",  # query
        "https://loki:3100/#frag",  # fragment
    ],
)
def test_housekeeping_loki_url_rejects_unusable_bases(url):
    with pytest.raises(ValidationError):
        GlobalSettings.model_validate({"housekeeping_loki_url": url})


def test_housekeeping_enabled_requires_loki_url():
    with pytest.raises(ValidationError):
        GlobalSettings.model_validate({"housekeeping_enabled": True})
    s = GlobalSettings.model_validate(
        {
            "housekeeping_enabled": True,
            "housekeeping_loki_url": "http://10.10.10.39:3100",
        }
    )
    assert s.require_loki_url() == "http://10.10.10.39:3100"


def test_require_loki_url_exposed_for_explicit_mode():
    """--housekeeping-only requests the feature without setting
    housekeeping_enabled, so the URL check must be callable directly."""
    disabled = GlobalSettings()
    assert disabled.housekeeping_enabled is False
    with pytest.raises(ValueError):
        disabled.require_loki_url()

    configured = GlobalSettings.model_validate({"housekeeping_loki_url": "https://loki.example"})
    assert configured.require_loki_url() == "https://loki.example"
    # validate_assignment is off, so a mutated instance must still be guarded.
    configured.housekeeping_loki_url = "http://u:p@loki:3100"
    with pytest.raises(ValueError):
        configured.require_loki_url()


def test_validate_loki_base_url_classmethod_exposed():
    assert GlobalSettings.validate_loki_base_url("") == ""
    assert GlobalSettings.validate_loki_base_url("http://loki:3100") == "http://loki:3100"
    with pytest.raises(ValueError):
        GlobalSettings.validate_loki_base_url("http://loki:3100?q=1")
    with pytest.raises(ValueError):
        GlobalSettings.validate_loki_base_url("http://u:p@loki:3100")


def test_housekeeping_exclude_list_entries_coerced_to_str():
    s = GlobalSettings.model_validate({"lxc_housekeeping_exclude_list": [121, "alpha/129"]})
    assert s.lxc_housekeeping_exclude_list == ["121", "alpha/129"]


def test_housekeeping_settings_load_from_yaml(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text(
        "housekeeping_enabled: true\n"
        "housekeeping_loki_url: http://10.10.10.39:3100\n"
        "housekeeping_local_retention_hours: 24\n"
        "housekeeping_journal_max_mb: 128\n"
        "housekeeping_journal_keep_free_mb: 256\n"
        "housekeeping_cache_interval_hours: 12\n"
        "housekeeping_backfill_budget_mb: 512\n"
        "lxc_housekeeping_exclude_list:\n"
        '  - 121\n'
        '  - "beta/129"\n'
    )
    s = GlobalSettings.load(f)
    assert s.housekeeping_enabled is True
    assert s.housekeeping_loki_url == "http://10.10.10.39:3100"
    assert s.housekeeping_local_retention_hours == 24
    assert s.housekeeping_journal_max_mb == 128
    assert s.housekeeping_journal_keep_free_mb == 256
    assert s.housekeeping_cache_interval_hours == 12
    assert s.housekeeping_backfill_budget_mb == 512
    assert s.lxc_housekeeping_exclude_list == ["121", "beta/129"]


def test_housekeeping_enabled_with_bad_url_from_yaml_rejected(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text("housekeeping_enabled: true\nhousekeeping_loki_url: 10.10.10.39:3100\n")
    with pytest.raises(ValidationError):
        GlobalSettings.load(f)


# --- housekeeping timer scheduling (installer interface) ---------------------

LOKI_URL = "http://loki.example.lan:3100"


@pytest.mark.parametrize(
    "timer, ordinary, expected",
    [
        (None, False, False),  # legacy default: off when ordinary is off
        (None, True, True),  # unset follows ordinary housekeeping_enabled
        (False, True, False),  # explicit false overrides an enabled ordinary run
        (True, False, True),  # explicit true schedules without ordinary runs
        (True, True, True),
    ],
)
def test_housekeeping_timer_effective_enabled_precedence(timer, ordinary, expected):
    raw = {"housekeeping_enabled": ordinary}
    if timer is not None:
        raw["housekeeping_timer_enabled"] = timer
    if ordinary or timer:
        raw["housekeeping_loki_url"] = LOKI_URL
    s = GlobalSettings.model_validate(raw)
    assert s.housekeeping_timer_effective_enabled is expected


def test_housekeeping_timer_enabled_requires_loki_url():
    """Scheduling requests the feature, so it needs an endpoint even when
    ordinary fleet housekeeping is disabled."""
    with pytest.raises(ValidationError):
        GlobalSettings.model_validate({"housekeeping_timer_enabled": True})
    s = GlobalSettings.model_validate({
        "housekeeping_enabled": False,
        "housekeeping_timer_enabled": True,
        "housekeeping_loki_url": LOKI_URL,
    })
    assert s.housekeeping_timer_effective_enabled is True
    assert s.require_loki_url() == LOKI_URL


def test_housekeeping_timer_disabled_still_requires_url_for_ordinary_runs():
    """Explicit false only silences the schedule — an ordinary opt-in still
    requests housekeeping, so the URL prerequisite still applies."""
    with pytest.raises(ValidationError):
        GlobalSettings.model_validate({
            "housekeeping_enabled": True,
            "housekeeping_timer_enabled": False,
        })


def test_housekeeping_timer_enabled_bad_url_still_rejected():
    with pytest.raises(ValidationError):
        GlobalSettings.model_validate({
            "housekeeping_timer_enabled": True,
            "housekeeping_loki_url": "loki:3100",
        })


def test_housekeeping_timer_targets_coerce_numeric_ids():
    s = GlobalSettings.model_validate({
        "housekeeping_timer_enabled": True,
        "housekeeping_loki_url": LOKI_URL,
        "housekeeping_timer_targets": [120, "default/123", "beta/121"],
    })
    assert s.housekeeping_timer_targets == ["120", "default/123", "beta/121"]




@pytest.mark.parametrize(
    "bad",
    [
        "120 123",  # whitespace would split the systemd argument
        "120\n121",  # control character would corrupt the unit file
        '1"20',  # quoting
        "$(id)",  # shell/expansion metacharacters
        "120%i",  # systemd specifier would expand
        "$HOST",  # systemd environment variable would expand
        "12\\0",  # backslash
        "default/120/121",  # at most one qualifier segment
        "/120",  # empty cluster segment
        "120/",  # empty id segment
        "",  # empty token
    ],
)
def test_housekeeping_timer_targets_reject_unsafe_tokens(bad):
    with pytest.raises(ValidationError):
        GlobalSettings.model_validate({
            "housekeeping_timer_enabled": True,
            "housekeeping_loki_url": LOKI_URL,
            "housekeeping_timer_targets": [bad],
        })




def test_housekeeping_timer_settings_load_from_yaml(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text(
        "housekeeping_enabled: false\n"
        f"housekeeping_loki_url: {LOKI_URL}\n"
        "housekeeping_timer_enabled: true\n"
        "housekeeping_timer_targets:\n"
        "  - 120\n"
        '  - "default/123"\n'
    )
    s = GlobalSettings.load(f)
    assert s.housekeeping_enabled is False
    assert s.housekeeping_timer_effective_enabled is True
    assert s.housekeeping_timer_targets == ["120", "default/123"]


def test_housekeeping_timer_enabled_without_url_from_yaml_rejected(tmp_path):
    f = tmp_path / "vars.yml"
    f.write_text("housekeeping_timer_enabled: true\n")
    with pytest.raises(ValidationError):
        GlobalSettings.load(f)
