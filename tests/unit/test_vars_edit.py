"""Tests for proxmox_fleet.vars_edit — comment-preserving vars.yml edits."""
import pytest

pytest.importorskip("ruamel.yaml")

import yaml  # noqa: E402

from proxmox_fleet.vars_edit import (  # noqa: E402
    VarsEditError,
    apply_changes,
    host_vars_delete,
    host_vars_write,
    read_raw,
    replace_raw,
)

SAMPLE = """\
# --- Proxmox API credentials (hand-written comment) ---
pve_api_user: "root@pam"
pve_api_token_secret: "s3cret"

# canary staging
canary_hosts: []
lxc_forks: 20

lxc_kuma_map:
  "100": "5"   # sonarr
"""


@pytest.fixture
def vars_file(tmp_path):
    path = tmp_path / "vars.yml"
    path.write_text(SAMPLE)
    return str(path)


def test_set_key_preserves_comments_and_quotes(vars_file):
    assert apply_changes(vars_file, set_keys={"lxc_forks": 5}) is True
    text = read_raw(vars_file)
    assert "# --- Proxmox API credentials (hand-written comment) ---" in text
    assert "# canary staging" in text
    assert "# sonarr" in text
    assert 'pve_api_user: "root@pam"' in text   # quoting style intact
    assert "lxc_forks: 5" in text


def test_list_add_and_remove(vars_file):
    apply_changes(vars_file, list_add={"canary_hosts": ["105", "media-vm"]})
    data = yaml.safe_load(read_raw(vars_file))
    assert data["canary_hosts"] == ["105", "media-vm"]
    # adding an existing entry is a no-op (str-compared)
    apply_changes(vars_file, list_add={"canary_hosts": ["105"]})
    assert yaml.safe_load(read_raw(vars_file))["canary_hosts"] == ["105", "media-vm"]
    apply_changes(vars_file, list_remove={"canary_hosts": ["105", "ghost"]})
    assert yaml.safe_load(read_raw(vars_file))["canary_hosts"] == ["media-vm"]


def test_map_set_and_remove_creates_missing_key(vars_file):
    apply_changes(vars_file, map_set={"vm_kuma_map": {"200": "7"}})
    apply_changes(vars_file, map_set={"lxc_kuma_map": {"101": "6"}})
    data = yaml.safe_load(read_raw(vars_file))
    assert data["vm_kuma_map"] == {"200": "7"}
    assert data["lxc_kuma_map"] == {"100": "5", "101": "6"}
    apply_changes(vars_file, map_remove={"lxc_kuma_map": ["100", "ghost"]})
    assert yaml.safe_load(read_raw(vars_file))["lxc_kuma_map"] == {"101": "6"}


def test_noop_change_returns_false_and_writes_nothing(vars_file, tmp_path):
    assert apply_changes(vars_file, list_remove={"canary_hosts": ["ghost"]}) is False
    assert list(tmp_path.glob("vars.yml.bak-*")) == []


def test_invalid_change_rejected_before_write(vars_file):
    before = read_raw(vars_file)
    with pytest.raises(VarsEditError, match="validation failed"):
        apply_changes(vars_file, set_keys={"lxc_forks": "not-a-number"})
    assert read_raw(vars_file) == before   # untouched


def test_backup_written_on_change(vars_file, tmp_path):
    apply_changes(vars_file, set_keys={"lxc_forks": 7})
    backups = list(tmp_path.glob("vars.yml.bak-*"))
    assert len(backups) == 1
    assert "lxc_forks: 20" in backups[0].read_text()


def test_replace_raw_validates(vars_file):
    with pytest.raises(VarsEditError, match="not valid YAML"):
        replace_raw(vars_file, "key: [unclosed")
    with pytest.raises(VarsEditError, match="mapping"):
        replace_raw(vars_file, "- just\n- a\n- list\n")
    with pytest.raises(VarsEditError, match="validation failed"):
        replace_raw(vars_file, "lxc_forks: banana\n")
    replace_raw(vars_file, "# new file\nlxc_forks: 3")
    assert read_raw(vars_file) == "# new file\nlxc_forks: 3\n"


def test_host_vars_write_and_delete(tmp_path):
    d = str(tmp_path / "host_vars")
    target = host_vars_write(d, "web-01", {
        "pre_update_cmd": "systemctl stop app",
        "maintenance_window": {"days": ["Sat"], "start": "02:00"},
    })
    data = yaml.safe_load(open(target).read())
    assert data["maintenance_window"]["days"] == ["Sat"]
    assert host_vars_delete(d, "web-01") is True
    assert host_vars_delete(d, "web-01") is False
