"""Tests for proxmox_fleet.inventory loaders (custom/nodes/vms/remote)."""

import pytest

from proxmox_fleet.inventory import (
    load_custom_hosts,
    load_proxmox_nodes,
    load_proxmox_vms,
    load_remote_hosts,
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
