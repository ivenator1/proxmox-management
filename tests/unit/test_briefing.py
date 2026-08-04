"""Tests for proxmox_fleet.briefing — the Python port of discord_briefing.j2.

Two layers:
1. Behavioural cases mirroring the retired discord_briefing.j2 tests against
   ``render_briefing(FleetState(...))``.
2. A GOLDEN parity test that compares ``render_briefing()`` against a captured
   fixture (tests/unit/data/briefing_golden.json). The fixture was seeded from
   the live discord_briefing.j2 render before the template was retired, so it
   locks the exact bytes that used to go to Discord.
"""
import json
import pathlib

import pytest

from proxmox_fleet.briefing import (
    briefing_title,
    discord_color,
    ntfy_title,
    prepare_body,
    render_briefing,
    should_notify,
)
from proxmox_fleet.models.state import FleetState

GOLDEN_PATH = pathlib.Path(__file__).parent / "data" / "briefing_golden.json"


def _state(**kw) -> FleetState:
    return FleetState.from_raw(kw)


def lxc(node="pve-01", name="sonarr", id="101", app="Updated: v4.0 → v4.1", os="OK", snap=True):
    return dict(node=node, name=name, id=id, app=app, os=os, snap=snap)


def node(node="pve-01", status="OK"):
    return dict(node=node, status=status)


def vm(node="pve-01", vmid="200", name="my-vm", status="UPDATED", **extra):
    return dict(node=node, vmid=vmid, name=name, status=status, **extra)


# --------------------------------------------------------------------------- #
# Behavioural cases (mirror test_discord_briefing.py)
# --------------------------------------------------------------------------- #

def test_node_header():
    assert "**pve-01: (OK)**" in render_briefing(_state(fleet_node_data=[node()]))


def test_updated_lxc_appears():
    out = render_briefing(_state(fleet_node_data=[node()], fleet_lxc_data=[lxc()]))
    assert "- sonarr (101) — Updated: v4.0 → v4.1" in out


def test_os_status_shown_when_non_empty():
    out = render_briefing(_state(fleet_node_data=[node()],
                                 fleet_lxc_data=[lxc(os="Updated (2 upgraded)")]))
    assert "| OS: Updated (2 upgraded)" in out


def test_os_status_hidden_when_empty_string():
    out = render_briefing(_state(fleet_node_data=[node()], fleet_lxc_data=[lxc(os="")]))
    assert "| OS:" not in out


def test_os_status_hidden_when_none_string():
    out = render_briefing(_state(fleet_node_data=[node()], fleet_lxc_data=[lxc(os="None")]))
    assert "| OS:" not in out


def test_no_snap_flag():
    out = render_briefing(_state(fleet_node_data=[node()], fleet_lxc_data=[lxc(snap=False)]))
    assert "*(no snap)*" in out


def test_snap_present_no_flag():
    out = render_briefing(_state(fleet_node_data=[node()], fleet_lxc_data=[lxc(snap=True)]))
    assert "*(no snap)*" not in out


def test_no_container_changes_shown():
    out = render_briefing(_state(fleet_node_data=[node()]))
    assert "*No container changes.*" in out


def test_node_reboot_reasons_are_rendered():
    record = dict(
        node(status="UPDATED (MANUAL REBOOT REQ)"),
        reboot_reasons=["NVIDIA module mismatch: loaded 550.54.14, installed 550.90.07"],
    )
    out = render_briefing(_state(fleet_node_data=[record]))
    assert (
        "- reboot required: NVIDIA module mismatch: loaded 550.54.14, installed 550.90.07"
        in out
    )


def test_node_without_reboot_reasons_keeps_legacy_rendering():
    assert (
        render_briefing(_state(fleet_node_data=[node()]))
        == "**pve-01: (OK)**\n- *No container changes.*"
    )


def test_ansible_manager_no_container_list():
    out = render_briefing(_state(fleet_node_data=[node(node="Ansible-Manager")]))
    assert "Ansible-Manager: (OK)" in out
    assert "*No container changes.*" not in out


def test_vm_entry_shown():
    out = render_briefing(_state(fleet_node_data=[node()], fleet_vm_data=[vm()]))
    assert "- VM" in out
    assert "  - my-vm (200) — UPDATED" in out


def test_vm_pkg_count_shown():
    out = render_briefing(_state(fleet_node_data=[node()], fleet_vm_data=[vm(pkg_count=5)]))
    assert "  - my-vm (200) — UPDATED (5 upgraded)" in out


def test_vm_pkg_count_absent_when_none():
    out = render_briefing(_state(fleet_node_data=[node()], fleet_vm_data=[vm(pkg_count=None)]))
    assert "upgraded" not in out


def test_vm_pkg_count_absent_when_zero():
    out = render_briefing(_state(fleet_node_data=[node()], fleet_vm_data=[vm(pkg_count=0)]))
    assert "upgraded" not in out


def test_lxc_and_vm_subheadings_both_shown():
    out = render_briefing(_state(fleet_node_data=[node()], fleet_lxc_data=[lxc()],
                                 fleet_vm_data=[vm()]))
    assert "- LXC" in out
    assert "- VM" in out


def test_remote_hosts_section():
    out = render_briefing(_state(fleet_remote_data=[dict(host="srv", status="UPDATED & REBOOTED")]))
    assert "**Remote Hosts**" in out
    assert "- srv — UPDATED & REBOOTED" in out


def test_custom_systems_section():
    out = render_briefing(_state(fleet_custom_data=[dict(host="nas", name="Gitea",
                                                         app="Updated: 1.20 → 1.21")]))
    assert "**Custom Systems**" in out
    assert "- **Gitea** (nas) — Updated: 1.20 → 1.21" in out


def test_error_log_section():
    out = render_briefing(_state(fleet_error_log=[dict(host="pve-01/lxc-101",
                                                       task="App update 101",
                                                       error="Permission denied")]))
    assert "**Error Log:**" in out
    assert "pve-01/lxc-101" in out
    assert "Permission denied" in out


def test_warnings_section():
    out = render_briefing(_state(fleet_warning_log=[dict(host="pve-01/lxc-101", task="Snapshot 101",
                                                         warning="snapshot failed")]))
    assert "**⚠️ Warnings**" in out
    assert "snapshot failed" in out


def test_lxc_filtered_per_node():
    out = render_briefing(_state(
        fleet_node_data=[node("pve-01"), node("pve-02")],
        fleet_lxc_data=[lxc(node="pve-01", name="sonarr", id="101"),
                        lxc(node="pve-02", name="radarr", id="102")],
    ))
    lines = out.splitlines()
    i1 = next(i for i, line in enumerate(lines) if "pve-01" in line)
    i2 = next(i for i, line in enumerate(lines) if "pve-02" in line)
    sec1, sec2 = "\n".join(lines[i1:i2]), "\n".join(lines[i2:])
    assert "sonarr" in sec1 and "radarr" not in sec1
    assert "radarr" in sec2 and "sonarr" not in sec2


# --------------------------------------------------------------------------- #
# Title / colour / should_notify
# --------------------------------------------------------------------------- #

def test_titles_and_color():
    assert briefing_title(True) == "⚠️ Briefing: Failures Detected"
    assert briefing_title(False) == "✅ Briefing: All Systems Clear"
    assert ntfy_title(True) == "Fleet Update: Failures Detected"
    assert ntfy_title(False) == "Fleet Update: All Systems Clear"
    assert discord_color(True) == 15158332
    assert discord_color(False) == 3066993


@pytest.mark.parametrize("force,dry,changed,failed,expected", [
    (False, False, False, False, False),
    (True, False, False, False, True),
    (False, True, False, False, True),
    (False, False, True, False, True),
    (False, False, False, True, True),
])
def test_should_notify(force, dry, changed, failed, expected):
    assert should_notify(force_notify=force, dry_run=dry, changed=changed, failed=failed) is expected


def test_prepare_body_trims():
    # Remote-only render starts with '\n\n'; prepare_body must strip it.
    body = prepare_body(_state(fleet_remote_data=[dict(host="h", status="OK")]))
    assert not body.startswith("\n")
    assert body.startswith("**Remote Hosts**")


def test_prepare_body_truncates_to_4000():
    hosts = [dict(host=f"host-{i:04d}", status="UPDATED") for i in range(500)]
    body = prepare_body(_state(fleet_remote_data=hosts))
    assert len(body) <= 4000
    assert body.endswith("\n...")


# --------------------------------------------------------------------------- #
# GOLDEN parity test — render_briefing == captured discord_briefing.j2 bytes
# --------------------------------------------------------------------------- #

_GOLDEN_SCENARIOS = {
    "empty": {},
    "one_node_idle": dict(fleet_node_data=[node()]),
    "lxc_updated": dict(fleet_node_data=[node()], fleet_lxc_data=[lxc()]),
    "lxc_os_none": dict(fleet_node_data=[node()], fleet_lxc_data=[lxc(os="None")]),
    "lxc_no_snap": dict(fleet_node_data=[node()], fleet_lxc_data=[lxc(snap=False)]),
    "vm_pkg": dict(fleet_node_data=[node()], fleet_vm_data=[vm(pkg_count=5)]),
    "vm_no_pkg": dict(fleet_node_data=[node()], fleet_vm_data=[vm(pkg_count=None)]),
    "manager": dict(fleet_node_data=[node(node="Ansible-Manager")]),
    "remote": dict(fleet_remote_data=[dict(host="srv", status="UPDATED & REBOOTED")]),
    "custom": dict(fleet_custom_data=[dict(host="nas", name="Gitea", app="Updated: 1.20 → 1.21")]),
    "errors": dict(fleet_error_log=[dict(host="pve-01/lxc-101", task="App update", error="denied")]),
    "warnings": dict(fleet_warning_log=[dict(host="h", task="Snap", warning="snapshot failed")]),
    "multi_node": dict(
        fleet_node_data=[node("pve-01"), node("pve-02")],
        fleet_lxc_data=[lxc(node="pve-01", name="sonarr", id="101"),
                        lxc(node="pve-02", name="radarr", id="102", snap=False)],
    ),
    "everything": dict(
        fleet_node_data=[node("pve-01"), node("pve-02", status="REBOOTED"),
                         node(node="Ansible-Manager")],
        fleet_lxc_data=[lxc(node="pve-01", name="sonarr", id="101"),
                        lxc(node="pve-02", name="radarr", id="102", os="None", snap=False)],
        fleet_vm_data=[vm(node="pve-01", pkg_count=3)],
        fleet_remote_data=[dict(host="srv", status="OK")],
        fleet_custom_data=[dict(host="nas", name="Gitea", app="OK")],
        fleet_error_log=[dict(host="x", task="t", error="e")],
        fleet_warning_log=[dict(host="y", task="u", warning="w")],
    ),
}


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_golden_covers_all_scenarios(golden):
    # Guard against a scenario being added here but not captured in the fixture.
    assert set(golden) == set(_GOLDEN_SCENARIOS), (
        "briefing_golden.json is out of sync with _GOLDEN_SCENARIOS — regenerate it"
    )


@pytest.mark.parametrize("name", list(_GOLDEN_SCENARIOS))
def test_golden_parity_with_captured_template(golden, name):
    scenario = _GOLDEN_SCENARIOS[name]
    actual = render_briefing(FleetState.from_raw(scenario))
    assert actual == golden[name], (
        f"briefing render diverged from captured .j2 bytes for scenario {name!r}"
    )
