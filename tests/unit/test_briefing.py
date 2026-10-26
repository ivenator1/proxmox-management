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
    format_disk_usage_warning,
    ntfy_title,
    prepare_body,
    render_briefing,
    should_notify,
)
from proxmox_fleet.models.state import (
    DiskUsageEntry,
    DiskUsageInsight,
    FleetState,
)

GOLDEN_PATH = pathlib.Path(__file__).parent / "data" / "briefing_golden.json"


def _state(**kw) -> FleetState:
    return FleetState.from_raw(kw)


def lxc(
    node="pve-01",
    name="sonarr",
    id="101",
    app="Updated: v4.0 → v4.1",
    os="OK",
    snap=True,
    **extra,
):
    return dict(node=node, name=name, id=id, app=app, os=os, snap=snap, **extra)


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


def test_nvidia_target_readiness_is_rendered_for_pending_reboot():
    record = dict(
        node(status="UPDATED (MANUAL REBOOT REQ)"),
        reboot_reasons=["kernel update: 7.0.14-14-pve → 7.0.14-15-pve"],
        checks={
            "target_kernel": "7.0.14-15-pve",
            "nvidia_post_reboot_ready": True,
        },
    )

    out = render_briefing(_state(fleet_node_data=[record]))

    assert "NVIDIA target 7.0.14-15-pve: DKMS/modules ready for reboot" in out


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


def test_alloy_status_renders_alongside_guest_status():
    out = render_briefing(_state(
        fleet_lxc_data=[lxc(app="OK", os="", alloy="Configured")],
        fleet_vm_data=[vm(status="UPDATED", alloy="Service repaired")],
    ))
    assert "sonarr (101) — OK | Alloy: Configured" in out
    assert "my-vm (200) — UPDATED | Alloy: Service repaired" in out


def test_alloy_only_status_renders_without_blank_update_status():
    out = render_briefing(_state(
        fleet_lxc_data=[lxc(app="", os="", snap=False, alloy="Installed")],
        fleet_vm_data=[vm(status="", alloy="Configured")],
    ))
    assert "sonarr (101) — Alloy: Installed" in out
    assert "my-vm (200) — Alloy: Configured" in out
    assert "*(no snap)*" not in out


def test_maintenance_report_preserves_empty_update_status_and_no_snapshot_marker():
    state = _state(fleet_lxc_data=[lxc(app="", os="", snap=False, housekeeping={
        "status": "Cleaned", "bytes_reclaimed": 6 * 1024 ** 3,
        "bytes_archived": 13 * 1024 ** 3, "files_pruned": 30,
    })])
    rendered = render_briefing(state)
    assert "Housekeeping: Cleaned (6.0 GiB reclaimed; 13.0 GiB archived)" in rendered
    assert "UPDATED" not in rendered
    assert "*(no snap)*" not in rendered
    assert not rendered.endswith("\n")
    assert state.lxc[0].app == "" and state.lxc[0].os == ""


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


def test_lxc_only_run_renders_guest_under_its_node():
    out = render_briefing(_state(
        fleet_lxc_data=[lxc(node="pve01", name="firecrawl", id="128", os="Updated (2 upgraded)")],
    ))

    assert "**pve01: (GUEST RESULTS)**" in out
    assert "- LXC" in out
    assert "firecrawl (128)" in out
    assert "OS: Updated (2 upgraded)" in out


def test_guest_only_run_groups_lxcs_and_vms_without_duplicate_node_headers():
    out = render_briefing(_state(
        fleet_lxc_data=[
            lxc(node="pve01", name="firecrawl", id="128"),
            lxc(node="pve02", name="logs", id="102"),
        ],
        fleet_vm_data=[vm(node="pve01", vmid="110", name="eve-ng")],
    ))

    assert out.count("**pve01: (GUEST RESULTS)**") == 1
    assert out.count("**pve02: (GUEST RESULTS)**") == 1
    pve01 = out.split("**pve02: (GUEST RESULTS)**", 1)[0]
    assert "firecrawl (128)" in pve01
    assert "eve-ng (110)" in pve01


# --------------------------------------------------------------------------- #
# Title / colour / should_notify
# --------------------------------------------------------------------------- #

def test_titles_and_color():
    assert briefing_title(True) == "⚠️ Briefing: Failures Detected"
    assert briefing_title(False) == "✅ Briefing: All Systems Clear"
    assert briefing_title(False, attention=True) == "⚠️ Briefing: Manual Attention Required"
    assert ntfy_title(True) == "Fleet Update: Failures Detected"
    assert ntfy_title(False) == "Fleet Update: All Systems Clear"
    assert ntfy_title(False, attention=True) == "Fleet Update: Manual Attention Required"
    assert discord_color(True) == 15158332
    assert discord_color(False) == 3066993
    assert discord_color(False, attention=True) == 16766720


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


def test_manual_section_is_grouped_between_nodes_and_remote_hosts():
    body = prepare_body(
        _state(
            fleet_node_data=[node()],
            fleet_remote_data=[dict(host="remote", status="OK")],
        ),
        manual_section=(
            "**Manual Systems: (ATTENTION REQUIRED)**\n"
            "- Updates / reboots\n"
            "  - **firewall** — 1.0 → 2.0"
        ),
    )

    assert body.index("**pve-01: (OK)**") < body.index("**Manual Systems")
    assert body.index("**Manual Systems") < body.index("**Remote Hosts**")


def test_prepare_body_truncates_to_4000():
    hosts = [dict(host=f"host-{i:04d}", status="UPDATED") for i in range(500)]
    body = prepare_body(_state(fleet_remote_data=hosts))
    assert len(body) <= 4000
    assert body.endswith("\n...")


# --------------------------------------------------------------------------- #
# Disk-space insight: compact formatter + leading priority block
# --------------------------------------------------------------------------- #

def _disk_entry(
    path="/var/log",
    allocated_bytes=14 * 1024**3,
    share_percent=70.0,
    max_expected_share_percent=20.0,
    kind="directory",
):
    return DiskUsageEntry(
        path=path,
        allocated_bytes=allocated_bytes,
        share_percent=share_percent,
        max_expected_share_percent=max_expected_share_percent,
        kind=kind,
    )


def _disk_insight(status="complete", *, used_bytes=20 * 1024**3, entries=None,
                  omitted=0, detail=""):
    return DiskUsageInsight(
        status=status,
        used_bytes=used_bytes,
        entries=[_disk_entry()] if entries is None else entries,
        omitted=omitted,
        detail=detail,
    )


def _enriched_warning(base="LXC 135 disk space low: 91% used (2.0 GiB free)", *,
                      host="pve-01/135", task="disk space", insight=None):
    insight = insight or _disk_insight()
    return dict(
        host=host,
        task=task,
        warning=format_disk_usage_warning(base, insight),
        disk_usage=insight.model_dump(),
    )


def test_disk_usage_warning_appends_numeric_oversized_entries_only():
    base = "LXC 135 disk space low: 91% used (2.0 GiB free)"
    # The ratio filter upstream supplies only qualifying consumers; the
    # formatter must not invent small entries of its own.
    insight = _disk_insight(entries=[_disk_entry("/var/log", 14 * 1024**3, 70.0)])
    text = format_disk_usage_warning(base, insight)
    assert text.startswith(base)
    assert "`/var/log`: 14.0 GiB (70% of used; limit 20%)" in text
    assert "/var/cache" not in text
    assert len(text) <= 512
    assert not text.endswith("\n")
    # The structured insight keeps every measured entry; display filtered only.
    assert [entry.path for entry in insight.entries] == ["/var/log"]


def test_disk_usage_warning_shows_at_most_three_entries_and_omitted_count():
    entries = [_disk_entry(f"/var/log/app{index}", 1024**3, 10.0) for index in range(5)]
    insight = _disk_insight(entries=entries)
    text = format_disk_usage_warning("Disk space low", insight)
    assert text.count("`/var/log/app") == 3
    assert "more oversized director" in text
    assert len(text) <= 512
    assert len(insight.entries) == 5


def test_disk_usage_warning_sanitizes_and_clips_display_paths():
    dirty_path = "/var/log/" + "x" * 100 + "`\n" + "tail"
    insight = _disk_insight(entries=[_disk_entry(dirty_path, 2 * 1024**3, 40.0)])
    text = format_disk_usage_warning("Disk space low", insight)
    assert "\n" not in text
    assert dirty_path not in text
    assert text.count("`") == 2       # exactly the code span around the label
    assert "…" in text
    assert len(text) <= 512
    assert insight.entries[0].path == dirty_path


def test_disk_usage_warning_labels_residual_and_root_file_kinds():
    insight = _disk_insight(entries=[
        _disk_entry("/var", 6 * 1024**3, 30.0, kind="directory-other"),
        _disk_entry("/", 2 * 1024**3, 10.0, kind="root-files"),
    ])
    text = format_disk_usage_warning("Disk space low", insight)
    assert "`other contents under /var`" in text
    assert "`files directly under /`" in text


def test_disk_usage_warning_partial_is_labelled_incomplete():
    text = format_disk_usage_warning(
        "Disk space low",
        _disk_insight(status="partial", entries=[_disk_entry()]),
    )
    assert "incomplete" in text
    assert "no dominant" not in text


def test_disk_usage_warning_unavailable_uses_bounded_reason_without_entries():
    insight = _disk_insight(
        status="unavailable",
        used_bytes=None,
        entries=[_disk_entry()],
        detail="mount table missing",
    )
    text = format_disk_usage_warning("Disk space low", insight)
    assert "unavailable" in text
    assert "mount table missing" in text
    assert "/var/log" not in text
    assert len(text) <= 512
    assert not text.endswith("\n")


def test_disk_usage_warning_quiet_complete_scan_has_short_note():
    text = format_disk_usage_warning("Disk space low", _disk_insight(entries=[]))
    assert "no dominant" in text


def test_disk_usage_warning_includes_policy_detail_without_second_warning():
    text = format_disk_usage_warning(
        "Disk space low",
        _disk_insight(entries=[], detail="generic disk thresholds"),
    )
    assert "generic disk thresholds" in text
    assert text.count("Disk space low") == 1


def test_disk_usage_warning_total_budget_with_long_paths():
    entries = [
        _disk_entry("/var/" + "z" * 70 + str(index), 5 * 1024**3, 25.0)
        for index in range(3)
    ]
    text = format_disk_usage_warning("Disk space low", _disk_insight(entries=entries))
    assert len(text) <= 512
    assert text.startswith("Disk space low")
    assert not text.endswith("\n")
    assert text.count(" of used; limit ") <= 3


def test_enriched_warning_priority_block_precedes_errors_and_survives_truncation():
    warning = _enriched_warning()
    state = _state(
        fleet_error_log=[dict(host="pve-01/lxc-135", task="App update", error="E" * 5000)],
        fleet_warning_log=[
            warning,
            dict(host="pve-01/lxc-120", task="Snapshot 120", warning="snapshot failed"),
        ],
    )
    before = state.model_dump()
    out = render_briefing(state)

    assert out.startswith("**⚠️ Disk space**")
    assert out.index("**⚠️ Disk space**") < out.index("**Error Log:**")
    assert out.count("`/var/log`") == 1
    assert "**⚠️ Warnings**" in out
    assert "snapshot failed" in out

    body = prepare_body(state)
    assert body.startswith("**⚠️ Disk space**")
    assert "`/var/log`" in body
    assert len(body) <= 4000

    # Rendering must not mutate state or drop unabridged metadata.
    assert state.model_dump() == before
    assert state.warnings[0].disk_usage.entries[0].path == "/var/log"
    assert state.warnings[0].disk_usage.entries[0].allocated_bytes == 14 * 1024**3


def test_disk_only_warning_has_no_generic_warnings_heading():
    out = render_briefing(_state(fleet_warning_log=[_enriched_warning()]))
    assert out.startswith("**⚠️ Disk space**")
    assert "**⚠️ Warnings**" not in out
    assert out.count("LXC 135 disk space low") == 1


def test_legacy_warning_without_disk_usage_keeps_historical_bytes():
    out = render_briefing(_state(fleet_warning_log=[dict(host="y", task="u", warning="w")]))
    assert out == "\n\n**⚠️ Warnings**\n- **y** at `u`: w"
    assert "**⚠️ Disk space**" not in out


def test_disk_priority_block_is_bounded_and_reports_history_count():
    warnings = [
        _enriched_warning(
            base="LXC %d disk space low: %s" % (index, "x" * 430),
            host=f"pve-01/{index}",
        )
        for index in range(6)
    ]
    state = _state(fleet_warning_log=warnings)
    out = render_briefing(state)
    assert len(out) <= 1600
    assert out.startswith("**⚠️ Disk space**")
    assert "more disk warning" in out
    assert out.count("disk space low") <= 3
    assert len(state.warnings) == 6

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
