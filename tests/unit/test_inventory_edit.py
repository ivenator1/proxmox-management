"""Tests for proxmox_fleet.inventory_edit — hosts.ini line editor."""
import pytest

from proxmox_fleet import inventory
from proxmox_fleet.inventory_edit import (
    InventoryEditError,
    add_host,
    ensure_inventory,
    list_hosts,
    remove_host,
)

SAMPLE = """\
# fleet inventory — hand-maintained comment
[proxmox_nodes]
pve-01 ansible_host=10.0.0.10
pve-02 ansible_host=10.0.0.20

[proxmox_nodes:vars]
ansible_user=root
ansible_ssh_private_key_file=~/.ssh/id_ed25519

[proxmox_vms]
media-vm  ansible_host=10.0.0.50  vmid=200  pve_node=pve-01

[remote_hosts]
; semicolon comment survives too

[custom_hosts]
db-01     ansible_host=10.0.0.59  custom_config=postgres
gitea-01  ansible_host=10.0.0.60  custom_config=gitea
"""


@pytest.fixture
def ini(tmp_path):
    path = tmp_path / "hosts.ini"
    path.write_text(SAMPLE)
    return str(path)


def test_list_hosts_matches_parser(ini):
    hosts = list_hosts(ini)
    assert [h["name"] for h in hosts["proxmox_nodes"]] == ["pve-01", "pve-02"]
    assert hosts["proxmox_vms"][0]["vars"]["vmid"] == "200"
    assert hosts["remote_hosts"] == []
    assert [h["name"] for h in hosts["custom_hosts"]] == ["db-01", "gitea-01"]


def test_list_hosts_includes_manual_group(ini):
    hosts = list_hosts(ini)
    assert "manual_update_hosts" in hosts
    assert hosts["manual_update_hosts"] == []  # missing section → empty list


def test_add_host_appends_to_group_block(ini):
    add_host(ini, "remote_hosts", "web-01", {"ansible_host": "192.168.1.5"})
    hosts = list_hosts(ini)
    assert hosts["remote_hosts"] == [
        {"name": "web-01", "vars": {"ansible_host": "192.168.1.5"}}]
    # the new line sits inside [remote_hosts], before [custom_hosts]
    text = open(ini).read()
    assert text.index("[remote_hosts]") < text.index("web-01") < text.index("[custom_hosts]")


def test_add_host_preserves_comments_vars_and_order(ini):
    add_host(ini, "custom_hosts", "wiki-01",
             {"ansible_host": "10.0.0.61", "custom_config": "wiki"})
    text = open(ini).read()
    assert "# fleet inventory — hand-maintained comment" in text
    assert "; semicolon comment survives too" in text
    assert "ansible_ssh_private_key_file=~/.ssh/id_ed25519" in text
    # existing custom hosts keep their (Phase 0a) order; new host appended last
    names = [h["name"] for h in list_hosts(ini)["custom_hosts"]]
    assert names == ["db-01", "gitea-01", "wiki-01"]
    # round-trips through the real loader
    specs = inventory.load_custom_hosts(ini, host_vars_dir="nonexistent")
    assert [s.name for s in specs] == names


def test_add_host_duplicate_rejected(ini):
    with pytest.raises(InventoryEditError, match="already exists"):
        add_host(ini, "proxmox_nodes", "pve-01", {"ansible_host": "10.0.0.99"})


def test_add_host_validation(ini):
    with pytest.raises(InventoryEditError, match="unknown group"):
        add_host(ini, "nonsense", "x", {})
    with pytest.raises(InventoryEditError, match="invalid host name"):
        add_host(ini, "remote_hosts", "bad name!", {})
    with pytest.raises(InventoryEditError, match="invalid var name"):
        add_host(ini, "remote_hosts", "ok", {"bad-key": "v"})
    with pytest.raises(InventoryEditError, match="without whitespace"):
        add_host(ini, "remote_hosts", "ok", {"ansible_host": "two words"})


def test_add_host_creates_missing_section(tmp_path):
    path = tmp_path / "hosts.ini"
    path.write_text("[proxmox_nodes]\npve-01 ansible_host=10.0.0.10\n")
    add_host(str(path), "remote_hosts", "web-01", {"ansible_host": "10.0.0.5"})
    assert list_hosts(str(path))["remote_hosts"][0]["name"] == "web-01"


def test_remove_host(ini):
    remove_host(ini, "custom_hosts", "db-01")
    names = [h["name"] for h in list_hosts(ini)["custom_hosts"]]
    assert names == ["gitea-01"]
    text = open(ini).read()
    assert "postgres" not in text
    assert "[custom_hosts:vars]" not in text  # never had one — no accidental add
    assert "; semicolon comment survives too" in text


def test_remove_host_missing_rejected(ini):
    with pytest.raises(InventoryEditError, match="not found"):
        remove_host(ini, "remote_hosts", "ghost")


def test_remove_does_not_touch_same_name_in_other_group(tmp_path):
    path = tmp_path / "hosts.ini"
    path.write_text("[proxmox_nodes]\nalpha ansible_host=1.1.1.1\n"
                    "[remote_hosts]\nalpha ansible_host=2.2.2.2\n")
    remove_host(str(path), "remote_hosts", "alpha")
    hosts = list_hosts(str(path))
    assert hosts["proxmox_nodes"][0]["name"] == "alpha"
    assert hosts["remote_hosts"] == []


def test_backup_written_and_pruned(ini, tmp_path):
    for i in range(13):
        add_host(ini, "remote_hosts", f"h{i}", {"ansible_host": f"10.1.0.{i}"})
    backups = sorted(tmp_path.glob("hosts.ini.bak-*"))
    assert len(backups) == 10   # pruned to the newest 10
    # the most recent backup is the state just before the last add
    assert "h11" in backups[-1].read_text()
    assert "h12" not in backups[-1].read_text()


def test_ensure_inventory_bootstrap(tmp_path):
    path = tmp_path / "hosts.ini"
    assert ensure_inventory(str(path)) is True
    text = path.read_text()
    for group in ("proxmox_nodes", "proxmox_vms", "remote_hosts", "custom_hosts", "manual_update_hosts"):
        assert f"[{group}]" in text
        assert f"[{group}:vars]" in text
    assert "ansible_user=root" in text
    # idempotent — second call leaves the file alone
    assert ensure_inventory(str(path)) is False
    # and the bootstrap can be enrolled into immediately
    add_host(str(path), "proxmox_nodes", "pve-01", {"ansible_host": "10.0.0.10"})
    assert list_hosts(str(path))["proxmox_nodes"][0]["name"] == "pve-01"
    add_host(str(path), "manual_update_hosts", "truenas-01", {"manual_adapter": "TrueNAS"})
    assert list_hosts(str(path))["manual_update_hosts"][0]["name"] == "truenas-01"


# --- manual_update_hosts group (editor boundary) ---

def test_add_host_manual_requires_adapter(ini):
    with pytest.raises(InventoryEditError, match="manual_adapter"):
        add_host(ini, "manual_update_hosts", "truenas-01", {"ansible_host": "10.0.0.60"})
    # with the adapter present it succeeds and round-trips through the loader
    add_host(ini, "manual_update_hosts", "truenas-01",
             {"manual_adapter": "TrueNAS", "ansible_host": "10.0.0.60"})
    hosts = list_hosts(ini)
    assert hosts["manual_update_hosts"] == [{
        "name": "truenas-01",
        "vars": {"manual_adapter": "TrueNAS", "ansible_host": "10.0.0.60"},
    }]
    specs = inventory.load_manual_update_hosts(ini, host_vars_dir="nonexistent")
    assert [s.name for s in specs] == ["truenas-01"]
    assert specs[0].manual_adapter == "TrueNAS"


def test_add_host_manual_adapter_blank_rejected(ini):
    with pytest.raises(InventoryEditError, match="without whitespace"):
        add_host(ini, "manual_update_hosts", "truenas-01", {"manual_adapter": " "})


def test_remove_host_manual(ini):
    add_host(ini, "manual_update_hosts", "truenas-01",
             {"manual_adapter": "TrueNAS", "ansible_host": "10.0.0.60"})
    remove_host(ini, "manual_update_hosts", "truenas-01")
    assert list_hosts(ini)["manual_update_hosts"] == []
    with pytest.raises(InventoryEditError, match="not found"):
        remove_host(ini, "manual_update_hosts", "truenas-01")


def test_add_host_manual_rejects_auto_overlap(ini):
    # pve-01 lives in [proxmox_nodes], db-01 in [custom_hosts] (sample inventory)
    with pytest.raises(InventoryEditError, match="proxmox_nodes"):
        add_host(ini, "manual_update_hosts", "pve-01", {"manual_adapter": "TrueNAS"})
    with pytest.raises(InventoryEditError, match="custom_hosts"):
        add_host(ini, "manual_update_hosts", "db-01", {"manual_adapter": "TrueNAS"})


def test_add_host_auto_rejects_manual_overlap(ini):
    add_host(ini, "manual_update_hosts", "truenas-01",
             {"manual_adapter": "TrueNAS", "ansible_host": "10.0.0.60"})
    with pytest.raises(InventoryEditError, match="manual_update_hosts"):
        add_host(ini, "remote_hosts", "truenas-01", {"ansible_host": "10.0.0.99"})
    with pytest.raises(InventoryEditError, match="manual_update_hosts"):
        add_host(ini, "proxmox_nodes", "truenas-01", {"ansible_host": "10.0.0.99"})


def test_add_host_preserves_cross_auto_duplicates(ini):
    """A host in several auto-update groups stays legal — only manual↔auto
    overlap is forbidden (matches validate_manual_update_overlap)."""
    add_host(ini, "custom_hosts", "pve-01", {"custom_config": "x"})  # pve-01 also in [proxmox_nodes]
    hosts = list_hosts(ini)
    assert "pve-01" in [h["name"] for h in hosts["custom_hosts"]]
    assert "pve-01" in [h["name"] for h in hosts["proxmox_nodes"]]
