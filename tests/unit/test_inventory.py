"""Tests for proxmox_fleet.inventory loaders (custom/nodes/vms/remote)."""

import pytest

from proxmox_fleet.inventory import (
    ManualUpdateHostSpec,
    load_custom_hosts,
    load_manual_update_hosts,
    load_proxmox_nodes,
    load_proxmox_vms,
    load_remote_hosts,
    validate_manual_update_overlap,
    validate_node_uniqueness,
)


def _write_ini(tmp_path, content: str) -> str:
    p = tmp_path / "hosts.ini"
    p.write_text(content)
    return str(p)


def _write_host_vars(tmp_path, host: str, content: str) -> None:
    d = tmp_path / "host_vars"
    d.mkdir(exist_ok=True)
    (d / f"{host}.yml").write_text(content)


# --- basic parsing ---

def test_empty_section_returns_empty_list(tmp_path):
    path = _write_ini(tmp_path, "[custom_hosts]\n")
    assert load_custom_hosts(path) == []


def test_missing_section_returns_empty_list(tmp_path):
    path = _write_ini(tmp_path, "[other_group]\nhost1\n")
    assert load_custom_hosts(path) == []


def test_single_host_inline_vars(tmp_path):
    path = _write_ini(
        tmp_path,
        "[custom_hosts]\n"
        "gitea-server ansible_host=10.0.0.60 custom_config=gitea\n",
    )
    specs = load_custom_hosts(path)
    assert len(specs) == 1
    assert specs[0].name == "gitea-server"
    assert specs[0].ansible_host == "10.0.0.60"
    assert specs[0].custom_config == "gitea"


def test_multiple_hosts_order_preserved(tmp_path):
    path = _write_ini(
        tmp_path,
        "[custom_hosts]\n"
        "db-01 ansible_host=10.0.0.59 custom_config=postgres\n"
        "app-01 ansible_host=10.0.0.60 custom_config=gitea\n",
    )
    specs = load_custom_hosts(path)
    assert [s.name for s in specs] == ["db-01", "app-01"]


def test_group_vars_section_skipped(tmp_path):
    path = _write_ini(
        tmp_path,
        "[custom_hosts]\n"
        "host-01 ansible_host=10.0.0.1 custom_config=foo\n"
        "[custom_hosts:vars]\n"
        "ansible_user=root\n",
    )
    specs = load_custom_hosts(path, host_vars_dir=str(tmp_path / "hv"))
    assert len(specs) == 1
    assert specs[0].name == "host-01"


# --- host_vars merging ---

def test_depends_on_from_host_vars(tmp_path):
    path = _write_ini(
        tmp_path,
        "[custom_hosts]\n"
        "app-01 ansible_host=10.0.0.2 custom_config=app\n",
    )
    _write_host_vars(tmp_path, "app-01", "depends_on:\n  - db-01\n")
    specs = load_custom_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert specs[0].depends_on == ["db-01"]


def test_maintenance_window_from_host_vars(tmp_path):
    path = _write_ini(
        tmp_path,
        "[custom_hosts]\n"
        "app-01 ansible_host=10.0.0.2 custom_config=app\n",
    )
    _write_host_vars(
        tmp_path,
        "app-01",
        "maintenance_window:\n  days: [Sat, Sun]\n  start: '02:00'\n  end: '04:00'\n  tz: UTC\n",
    )
    specs = load_custom_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))
    from proxmox_fleet.models.config import MaintenanceWindow
    mw = specs[0].maintenance_window
    assert isinstance(mw, MaintenanceWindow)
    assert mw.days == ["Sat", "Sun"]
    assert mw.start == "02:00"


def test_custom_overrides_from_host_vars(tmp_path):
    path = _write_ini(
        tmp_path,
        "[custom_hosts]\n"
        "app-01 ansible_host=10.0.0.2 custom_config=app\n",
    )
    _write_host_vars(tmp_path, "app-01", "custom_overrides:\n  reboot: true\n")
    specs = load_custom_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert specs[0].custom_overrides == {"reboot": True}


def test_missing_host_vars_file_gives_defaults(tmp_path):
    path = _write_ini(
        tmp_path,
        "[custom_hosts]\n"
        "app-01 ansible_host=10.0.0.2 custom_config=app\n",
    )
    specs = load_custom_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert specs[0].depends_on == []
    assert specs[0].maintenance_window is None
    assert specs[0].custom_overrides == {}


# --- ansible_host defaults ---

def test_ansible_host_defaults_to_name_when_absent(tmp_path):
    path = _write_ini(
        tmp_path,
        "[custom_hosts]\n"
        "myhost custom_config=foo\n",
    )
    specs = load_custom_hosts(path)
    assert specs[0].ansible_host == "myhost"


# --- load_proxmox_nodes ---

def test_proxmox_nodes_basic(tmp_path):
    path = _write_ini(
        tmp_path,
        "[proxmox_nodes]\n"
        "pve-01 ansible_host=10.0.0.10\n"
        "pve-02 ansible_host=10.0.0.20\n",
    )
    nodes = load_proxmox_nodes(path)
    assert [n["name"] for n in nodes] == ["pve-01", "pve-02"]
    assert nodes[0]["ansible_host"] == "10.0.0.10"


def test_proxmox_nodes_vars_section_not_parsed_as_hosts(tmp_path):
    path = _write_ini(
        tmp_path,
        "[proxmox_nodes]\n"
        "pve-01 ansible_host=10.0.0.10\n"
        "[proxmox_nodes:vars]\n"
        "ansible_user=root\n"
        "ansible_ssh_private_key_file=~/.ssh/id_ed25519\n"
        "ansible_python_interpreter=/usr/bin/python3\n",
    )
    nodes = load_proxmox_nodes(path)
    assert len(nodes) == 1
    assert nodes[0]["name"] == "pve-01"


def test_proxmox_nodes_ansible_host_defaults_to_name(tmp_path):
    path = _write_ini(tmp_path, "[proxmox_nodes]\npve-01\n")
    nodes = load_proxmox_nodes(path)
    assert nodes[0]["ansible_host"] == "pve-01"


def test_proxmox_nodes_missing_section_returns_empty(tmp_path):
    path = _write_ini(tmp_path, "[other_group]\nhost1\n")
    assert load_proxmox_nodes(path) == []


def test_proxmox_node_nvidia_host_defaults_false(tmp_path):
    path = _write_ini(tmp_path, "[proxmox_nodes]\npve-01\n")
    assert load_proxmox_nodes(path)[0]["nvidia_host"] is False


def test_proxmox_node_nvidia_host_inline_true(tmp_path):
    path = _write_ini(tmp_path, "[proxmox_nodes]\npve-01 nvidia_host=true\n")
    assert load_proxmox_nodes(path)[0]["nvidia_host"] is True


def test_proxmox_node_nvidia_host_from_host_vars(tmp_path):
    path = _write_ini(tmp_path, "[proxmox_nodes]\npve-01\n")
    _write_host_vars(tmp_path, "pve-01", "nvidia_host: true\n")
    nodes = load_proxmox_nodes(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert nodes[0]["nvidia_host"] is True


def test_proxmox_node_inline_nvidia_host_overrides_host_vars(tmp_path):
    path = _write_ini(tmp_path, "[proxmox_nodes]\npve-01 nvidia_host=false\n")
    _write_host_vars(tmp_path, "pve-01", "nvidia_host: true\n")
    nodes = load_proxmox_nodes(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert nodes[0]["nvidia_host"] is False


def test_proxmox_nodes_ansible_host_from_host_vars(tmp_path):
    """Regression: a node whose IP lives only in host_vars must resolve to the IP,
    not the bare name — that value becomes the snapshot API api_host (must be an IP)."""
    path = _write_ini(tmp_path, "[proxmox_nodes]\npve-01\n")
    _write_host_vars(tmp_path, "pve-01", "ansible_host: 10.0.0.10\n")
    nodes = load_proxmox_nodes(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert nodes[0]["ansible_host"] == "10.0.0.10"


def test_proxmox_nodes_inline_wins_over_host_vars(tmp_path):
    path = _write_ini(tmp_path, "[proxmox_nodes]\npve-01 ansible_host=10.0.0.10\n")
    _write_host_vars(tmp_path, "pve-01", "ansible_host: 9.9.9.9\n")
    nodes = load_proxmox_nodes(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert nodes[0]["ansible_host"] == "10.0.0.10"


# --- cluster var (multi-cluster) ---

def test_proxmox_nodes_cluster_defaults(tmp_path):
    path = _write_ini(tmp_path, "[proxmox_nodes]\npve-01 ansible_host=10.0.0.10\n")
    nodes = load_proxmox_nodes(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert nodes[0]["cluster"] == "default"


def test_proxmox_nodes_cluster_inline_var(tmp_path):
    path = _write_ini(
        tmp_path,
        "[proxmox_nodes]\n"
        "alpha-01 ansible_host=10.0.0.10 cluster=alpha\n"
        "beta-01 ansible_host=10.1.0.10 cluster=beta\n",
    )
    nodes = load_proxmox_nodes(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert [n["cluster"] for n in nodes] == ["alpha", "beta"]


def test_proxmox_nodes_cluster_from_host_vars_inline_wins(tmp_path):
    path = _write_ini(
        tmp_path,
        "[proxmox_nodes]\npve-01\npve-02 cluster=beta\n",
    )
    _write_host_vars(tmp_path, "pve-01", "cluster: alpha\n")
    _write_host_vars(tmp_path, "pve-02", "cluster: gamma\n")
    nodes = load_proxmox_nodes(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert nodes[0]["cluster"] == "alpha"   # host_vars fallback
    assert nodes[1]["cluster"] == "beta"    # inline wins


def test_validate_node_uniqueness_ok():
    validate_node_uniqueness([
        {"name": "pve-01", "ansible_host": "10.0.0.1", "cluster": "alpha"},
        {"name": "pve-02", "ansible_host": "10.0.0.2", "cluster": "beta"},
    ])


def test_validate_node_uniqueness_duplicate_name_fails_loud():
    with pytest.raises(SystemExit) as exc:
        validate_node_uniqueness([
            {"name": "pve-01", "ansible_host": "10.0.0.1", "cluster": "alpha"},
            {"name": "pve-01", "ansible_host": "10.1.0.1", "cluster": "beta"},
        ])
    assert "pve-01" in str(exc.value)


def test_validate_node_uniqueness_shared_ip_warns(capsys):
    validate_node_uniqueness([
        {"name": "pve-01", "ansible_host": "10.0.0.1", "cluster": "alpha"},
        {"name": "pve-02", "ansible_host": "10.0.0.1", "cluster": "beta"},
    ])
    assert "10.0.0.1" in capsys.readouterr().err


# --- load_proxmox_vms ---

def test_proxmox_vms_inline_vars(tmp_path):
    path = _write_ini(
        tmp_path,
        "[proxmox_vms]\n"
        "vm-web ansible_host=10.0.0.30 vmid=200 pve_node=pve-01\n",
    )
    vms = load_proxmox_vms(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert len(vms) == 1
    assert vms[0].name == "vm-web"
    assert vms[0].ansible_host == "10.0.0.30"
    assert vms[0].vmid == "200"
    assert vms[0].pve_node == "pve-01"


def test_proxmox_vms_from_host_vars(tmp_path):
    path = _write_ini(tmp_path, "[proxmox_vms]\nvm-web\n")
    _write_host_vars(
        tmp_path, "vm-web",
        "ansible_host: 10.0.0.30\nvmid: 200\npve_node: pve-02\n",
    )
    vms = load_proxmox_vms(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert vms[0].vmid == "200"
    assert vms[0].pve_node == "pve-02"
    assert vms[0].ansible_host == "10.0.0.30"


def test_proxmox_vms_missing_section_returns_empty(tmp_path):
    path = _write_ini(tmp_path, "[remote_hosts]\n")
    assert load_proxmox_vms(path) == []


def test_proxmox_vms_cluster_var(tmp_path):
    path = _write_ini(
        tmp_path,
        "[proxmox_vms]\n"
        "vm-a ansible_host=10.0.0.30 vmid=200 cluster=beta\n"
        "vm-b ansible_host=10.0.0.31 vmid=200\n",
    )
    _write_host_vars(tmp_path, "vm-b", "cluster: alpha\n")
    vms = load_proxmox_vms(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert vms[0].cluster == "beta"
    assert vms[1].cluster == "alpha"


def test_proxmox_vms_cluster_defaults_empty(tmp_path):
    """Empty means "infer" (from pve_node or discovery) — not DEFAULT_CLUSTER."""
    path = _write_ini(tmp_path, "[proxmox_vms]\nvm-a vmid=200 pve_node=pve-01\n")
    assert load_proxmox_vms(path, host_vars_dir=str(tmp_path / "host_vars"))[0].cluster == ""


# --- load_remote_hosts ---

def test_remote_hosts_inline_and_host_vars(tmp_path):
    path = _write_ini(tmp_path, "[remote_hosts]\nweb-01 ansible_host=10.0.0.40\n")
    _write_host_vars(
        tmp_path, "web-01",
        "pre_update_cmd: systemctl stop app\n"
        "maintenance_window:\n  start: '01:00'\n  end: '02:00'\n",
    )
    hosts = load_remote_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert len(hosts) == 1
    assert hosts[0].name == "web-01"
    assert hosts[0].ansible_host == "10.0.0.40"
    assert hosts[0].pre_update_cmd == "systemctl stop app"
    assert hosts[0].maintenance_window.start == "01:00"


def test_remote_hosts_order_preserved(tmp_path):
    path = _write_ini(
        tmp_path,
        "[remote_hosts]\nhost-b\nhost-a\n",
    )
    hosts = load_remote_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert [h.name for h in hosts] == ["host-b", "host-a"]


def test_remote_hosts_missing_section_returns_empty(tmp_path):
    path = _write_ini(tmp_path, "[proxmox_nodes]\npve-01\n")
    assert load_remote_hosts(path) == []


# --- canary flag (staged rollout) ---

def test_canary_defaults_false(tmp_path):
    path = _write_ini(tmp_path, "[remote_hosts]\nweb-01\n[proxmox_vms]\nvm-01 vmid=200\n")
    assert load_remote_hosts(path)[0].canary is False
    assert load_proxmox_vms(path)[0].canary is False


def test_canary_inline_var_parsed(tmp_path):
    path = _write_ini(
        tmp_path,
        "[remote_hosts]\nweb-01 canary=true\nweb-02 canary=false\n"
        "[proxmox_vms]\nvm-01 vmid=200 canary=true\n",
    )
    hosts = load_remote_hosts(path)
    assert hosts[0].canary is True
    assert hosts[1].canary is False         # string 'false' must not be truthy
    assert load_proxmox_vms(path)[0].canary is True


def test_canary_from_host_vars(tmp_path):
    path = _write_ini(tmp_path, "[remote_hosts]\nweb-01\n")
    hv = tmp_path / "host_vars"
    hv.mkdir()
    (hv / "web-01.yml").write_text("canary: true\n")
    hosts = load_remote_hosts(path, host_vars_dir=str(hv))
    assert hosts[0].canary is True


# --- load_manual_update_hosts ---

def test_manual_update_hosts_basic_parse(tmp_path):
    path = _write_ini(
        tmp_path,
        "[manual_update_hosts]\n"
        "truenas-01 ansible_host=10.0.0.60 manual_adapter=TrueNAS\n",
    )
    specs = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert len(specs) == 1
    s = specs[0]
    assert isinstance(s, ManualUpdateHostSpec)
    assert s.name == "truenas-01"
    assert s.ansible_host == "10.0.0.60"
    assert s.manual_adapter == "TrueNAS"
    assert s.display_name is None
    assert s.apply_hint is None


def test_manual_update_hosts_order_preserved(tmp_path):
    path = _write_ini(
        tmp_path,
        "[manual_update_hosts]\n"
        "opnsense-01 manual_adapter=OPNsense\n"
        "truenas-01 manual_adapter=TrueNAS\n",
    )
    specs = load_manual_update_hosts(path)
    assert [s.name for s in specs] == ["opnsense-01", "truenas-01"]


def test_manual_update_hosts_missing_section_returns_empty(tmp_path):
    path = _write_ini(tmp_path, "[remote_hosts]\nweb-01\n")
    assert load_manual_update_hosts(path) == []


def test_manual_update_hosts_vars_section_not_parsed_as_hosts(tmp_path):
    path = _write_ini(
        tmp_path,
        "[manual_update_hosts]\n"
        "truenas-01 manual_adapter=TrueNAS\n"
        "[manual_update_hosts:vars]\n"
        "ansible_user=root\n",
    )
    specs = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "hv"))
    assert [s.name for s in specs] == ["truenas-01"]


def test_manual_update_hosts_adapter_from_host_vars(tmp_path):
    path = _write_ini(tmp_path, "[manual_update_hosts]\ntruenas-01\n")
    _write_host_vars(tmp_path, "truenas-01", "manual_adapter: TrueNAS\n")
    specs = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert specs[0].manual_adapter == "TrueNAS"


def test_manual_update_hosts_inline_adapter_wins_over_host_vars(tmp_path):
    path = _write_ini(tmp_path, "[manual_update_hosts]\ntruenas-01 manual_adapter=TrueNAS\n")
    _write_host_vars(tmp_path, "truenas-01", "manual_adapter: OPNsense\n")
    specs = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert specs[0].manual_adapter == "TrueNAS"


def test_manual_update_hosts_ansible_host_defaults_to_name(tmp_path):
    path = _write_ini(tmp_path, "[manual_update_hosts]\ntruenas-01 manual_adapter=TrueNAS\n")
    assert load_manual_update_hosts(path)[0].ansible_host == "truenas-01"


def test_manual_update_hosts_ansible_host_inline_wins_over_host_vars(tmp_path):
    path = _write_ini(tmp_path, "[manual_update_hosts]\ntruenas-01 ansible_host=10.0.0.60\n")
    _write_host_vars(tmp_path, "truenas-01", "ansible_host: 9.9.9.9\nmanual_adapter: TrueNAS\n")
    specs = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert specs[0].ansible_host == "10.0.0.60"


def test_manual_update_hosts_optional_fields_from_host_vars(tmp_path):
    path = _write_ini(tmp_path, "[manual_update_hosts]\ntruenas-01 manual_adapter=TrueNAS\n")
    _write_host_vars(
        tmp_path,
        "truenas-01",
        "display_name: TrueNAS Main\napply_hint: apply within 7 days\n",
    )
    s = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))[0]
    assert s.display_name == "TrueNAS Main"
    assert s.apply_hint == "apply within 7 days"


def test_manual_update_hosts_inline_optional_fields_win(tmp_path):
    path = _write_ini(
        tmp_path,
        "[manual_update_hosts]\ntruenas-01 manual_adapter=TrueNAS display_name=Prod\n",
    )
    _write_host_vars(tmp_path, "truenas-01", "display_name: Backup\n")
    s = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))[0]
    assert s.display_name == "Prod"


def test_manual_update_hosts_blank_optional_fields_become_none(tmp_path):
    path = _write_ini(tmp_path, "[manual_update_hosts]\ntruenas-01 manual_adapter=TrueNAS\n")
    _write_host_vars(tmp_path, "truenas-01", "display_name: ''\napply_hint: ''\n")
    s = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))[0]
    assert s.display_name is None
    assert s.apply_hint is None


# --- api transport fields (manual_adapter=*_api only) ---

def test_manual_update_hosts_api_fields_inline(tmp_path):
    path = _write_ini(
        tmp_path,
        "[manual_update_hosts]\n"
        "nas ansible_host=10.0.0.30 manual_adapter=truenas_scale_api "
        "api_url=https://10.0.0.30 api_key=KEY api_secret=SEC verify_ssl=false\n",
    )
    s = load_manual_update_hosts(path)[0]
    assert s.api_url == "https://10.0.0.30"
    assert s.api_key == "KEY"
    assert s.api_secret == "SEC"
    assert s.verify_ssl is False


def test_manual_update_hosts_api_fields_from_host_vars(tmp_path):
    path = _write_ini(tmp_path, "[manual_update_hosts]\nopn manual_adapter=opnsense_api\n")
    _write_host_vars(
        tmp_path,
        "opn",
        "api_url: https://opn.lan\n"
        "api_key: HVKEY\n"
        "api_secret: HVSEC\n"
        "verify_ssl: true\n",
    )
    s = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))[0]
    assert s.api_url == "https://opn.lan"
    assert s.api_key == "HVKEY"
    assert s.api_secret == "HVSEC"
    assert s.verify_ssl is True


def test_manual_update_hosts_inline_api_field_wins_over_host_vars(tmp_path):
    path = _write_ini(
        tmp_path,
        "[manual_update_hosts]\ntruenas-01 manual_adapter=truenas_scale_api verify_ssl=false\n",
    )
    _write_host_vars(
        tmp_path,
        "truenas-01",
        "api_url: https://hv.lan\napi_key: HVKEY\nverify_ssl: true\n",
    )
    s = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))[0]
    assert s.api_url == "https://hv.lan"  # not overridden inline → host_vars wins
    assert s.api_key == "HVKEY"
    assert s.verify_ssl is False  # inline false beats host_vars true


def test_manual_update_hosts_verify_ssl_true_forms(tmp_path):
    cases = [
        ("verify_ssl=true", True),
        ("verify_ssl=yes", True),
        ("verify_ssl=1", True),
        ("verify_ssl=on", True),
        ("verify_ssl=false", False),
        ("verify_ssl=no", False),
        ("verify_ssl=0", False),
        ("verify_ssl=off", False),
    ]
    for i, (inline, expected) in enumerate(cases):
        path = _write_ini(
            tmp_path,
            f"[manual_update_hosts]\nhost-{i} manual_adapter=opnsense_api {inline}\n",
        )
        s = load_manual_update_hosts(path)[0]
        assert s.verify_ssl is expected, f"{inline} → {s.verify_ssl}"


def test_manual_update_hosts_verify_ssl_yaml_bool_and_strings(tmp_path):
    # YAML `false` parses to a real Python False; quoted strings stay strings.
    path = _write_ini(tmp_path, "[manual_update_hosts]\nopn manual_adapter=opnsense_api\n")
    _write_host_vars(
        tmp_path,
        "opn",
        "verify_ssl: false\n",
    )
    s = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))[0]
    assert s.verify_ssl is False
    _write_host_vars(
        tmp_path,
        "opn",
        "verify_ssl: 'true'\n",
    )
    s = load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))[0]
    assert s.verify_ssl is True


def test_manual_update_hosts_invalid_verify_ssl_fails_loud(tmp_path):
    path = _write_ini(
        tmp_path,
        "[manual_update_hosts]\ntruenas-01 manual_adapter=truenas_scale_api verify_ssl=maybe\n",
    )
    with pytest.raises(SystemExit, match="verify_ssl"):
        load_manual_update_hosts(path)


def test_manual_update_hosts_invalid_verify_ssl_from_host_vars_fails_loud(tmp_path):
    path = _write_ini(tmp_path, "[manual_update_hosts]\ntruenas-01 manual_adapter=TrueNAS\n")
    _write_host_vars(tmp_path, "truenas-01", "verify_ssl: maybe\n")
    with pytest.raises(SystemExit, match="verify_ssl"):
        load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))


def test_manual_update_hosts_api_fields_default_when_absent(tmp_path):
    path = _write_ini(tmp_path, "[manual_update_hosts]\ntruenas-01 manual_adapter=TrueNAS\n")
    s = load_manual_update_hosts(path)[0]
    assert s.api_url is None
    assert s.api_key is None
    assert s.api_secret is None
    assert s.verify_ssl is True


# --- required manual_adapter (fails at load time, before host contact) ---

def test_manual_update_hosts_missing_adapter_fails_before_contact(tmp_path):
    path = _write_ini(tmp_path, "[manual_update_hosts]\ntruenas-01\n")
    with pytest.raises(SystemExit) as exc:
        load_manual_update_hosts(path)
    assert "truenas-01" in str(exc.value)
    assert "manual_adapter" in str(exc.value)


def test_manual_update_hosts_blank_inline_adapter_fails(tmp_path):
    # a blank inline value never survives _parse_inline_vars → same as missing
    path = _write_ini(tmp_path, "[manual_update_hosts]\ntruenas-01 manual_adapter=\n")
    with pytest.raises(SystemExit) as exc:
        load_manual_update_hosts(path)
    assert "manual_adapter" in str(exc.value)


def test_manual_update_hosts_blank_host_vars_adapter_fails(tmp_path):
    path = _write_ini(tmp_path, "[manual_update_hosts]\ntruenas-01\n")
    _write_host_vars(tmp_path, "truenas-01", "manual_adapter: ''\n")
    with pytest.raises(SystemExit) as exc:
        load_manual_update_hosts(path, host_vars_dir=str(tmp_path / "host_vars"))
    assert "manual_adapter" in str(exc.value)


# --- manual/auto overlap validation (pure parsing, pre-executor) ---

def test_manual_update_overlap_ok_when_no_overlap(tmp_path):
    path = _write_ini(
        tmp_path,
        "[manual_update_hosts]\ntruenas-01 manual_adapter=TrueNAS\n"
        "[remote_hosts]\nweb-01\n",
    )
    validate_manual_update_overlap(path)  # must not raise


def test_manual_update_overlap_empty_manual_ok(tmp_path):
    path = _write_ini(tmp_path, "[remote_hosts]\nweb-01\n[custom_hosts]\napp-01 custom_config=x\n")
    validate_manual_update_overlap(path)  # must not raise


def test_manual_update_overlap_rejects_each_auto_group(tmp_path):
    for group in ("remote_hosts", "proxmox_vms", "custom_hosts", "proxmox_nodes"):
        path = _write_ini(
            tmp_path,
            f"[manual_update_hosts]\nshared manual_adapter=TrueNAS\n[{group}]\nshared\n",
        )
        with pytest.raises(SystemExit) as exc:
            validate_manual_update_overlap(path)
        msg = str(exc.value)
        assert "shared" in msg
        assert "[manual_update_hosts]" in msg
        assert f"[{group}]" in msg


def test_manual_update_overlap_names_multiple_groups(tmp_path):
    path = _write_ini(
        tmp_path,
        "[manual_update_hosts]\nshared manual_adapter=TrueNAS\n"
        "[remote_hosts]\nshared\n"
        "[custom_hosts]\nshared\n",
    )
    with pytest.raises(SystemExit) as exc:
        validate_manual_update_overlap(path)
    msg = str(exc.value)
    assert "[remote_hosts]" in msg
    assert "[custom_hosts]" in msg


def test_manual_update_overlap_allows_cross_auto_duplicates(tmp_path):
    """A host in several auto-update groups is legal — only manual↔auto overlap
    is forbidden, and the validator never contacts a host (pure parsing)."""
    path = _write_ini(
        tmp_path,
        "[manual_update_hosts]\ntruenas-01 manual_adapter=TrueNAS\n"
        "[remote_hosts]\nshared\n"
        "[custom_hosts]\nshared\n",
    )
    validate_manual_update_overlap(path)  # must not raise


def test_manual_update_overlap_ignores_commented_hosts(tmp_path):
    """Commented-out lines are inactive and must not trip the overlap check."""
    path = _write_ini(
        tmp_path,
        "[manual_update_hosts]\ntruenas-01 manual_adapter=TrueNAS\n"
        "[remote_hosts]\n# truenas-01\nweb-01\n",
    )
    validate_manual_update_overlap(path)  # must not raise
