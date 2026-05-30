"""Tests for proxmox_fleet.inventory.load_custom_hosts."""
import pytest

from proxmox_fleet.inventory import load_custom_hosts


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
    mw = specs[0].maintenance_window
    assert mw is not None
    assert mw["days"] == ["Sat", "Sun"]
    assert mw["start"] == "02:00"


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
