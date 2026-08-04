"""Tests for proxmox_fleet.ledger — the per-host hosts.json accumulator (PR3).

Covers read_ledger's fresh recovery (missing / corrupt / legacy shapes), the
per-bucket run identities (lxc node/id — NOT container name, vm name, remote
host, node/manager node; custom excluded), last_changed_ts gating on the
shared applied-update predicate, the scan-side OS-upgrade events (version_id
then pretty_name, first-observation baseline, newest-100 cap), and the
durability guarantee that pruning run/pending files never loses ledger state.
"""

import json

from proxmox_fleet.history import write_history
from proxmox_fleet.ledger import (
    _EVENTS_CAP,
    _host_key,
    _scan_lxc_key,
    observe_run,
    observe_scan,
    read_ledger,
)
from proxmox_fleet.models.state import FleetState


def _run(records, ts="20260101T000000000000Z"):
    """A run summary dict with the given per-bucket record lists."""
    summary = {"timestamp": ts, "changed": False, "failed": False, "counts": {}, "errors": [], "warnings": []}
    summary.update(records)
    return summary


def _scan(ts="20260101T000000000000Z", hosts=None, lxc=None):
    return {"timestamp": ts, "hosts": hosts or {}, "lxc": lxc or {}}


# --- read_ledger: fresh recovery ------------------------------------------- #


def test_read_ledger_missing_dir_is_fresh(tmp_path):
    assert read_ledger(tmp_path / "nope") == {"hosts": {}, "events": []}


def test_read_ledger_corrupt_json_is_fresh(tmp_path):
    (tmp_path / "hosts.json").write_text("{not json!!", encoding="utf-8")
    assert read_ledger(tmp_path) == {"hosts": {}, "events": []}


def test_read_ledger_deeply_nested_json_is_fresh(tmp_path):
    """Deeply nested JSON overflows the decoder's recursion limit
    (RecursionError — a distinct failure mode from syntax corruption) —
    read_ledger must not raise and returns a fresh ledger. Depth is generated
    programmatically, well above every supported interpreter's practical limit
    (~52k on 3.14; far lower on the recursive 3.10–3.12 scanners)."""
    depth = 100_000
    (tmp_path / "hosts.json").write_text('{"hosts": ' + "[" * depth + "]" * depth + "}", encoding="utf-8")
    assert read_ledger(tmp_path) == {"hosts": {}, "events": []}


def test_read_ledger_non_object_payload_is_fresh(tmp_path):
    (tmp_path / "hosts.json").write_text(json.dumps([1, 2]), encoding="utf-8")
    assert read_ledger(tmp_path) == {"hosts": {}, "events": []}


def test_read_ledger_roundtrips(tmp_path):
    observe_run(tmp_path, _run({"remote": [{"host": "web", "status": "UPDATED"}]}))
    data = read_ledger(tmp_path)
    assert data["hosts"]["web"]["last_status"] == "UPDATED"
    assert data["events"] == []


def test_read_ledger_normalizes_missing_keys(tmp_path):
    """A hand-written object missing one key is normalised, not discarded."""
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": {"web": {"last_run_ts": "T"}}}), encoding="utf-8")
    data = read_ledger(tmp_path)
    assert data["hosts"]["web"]["last_run_ts"] == "T"
    assert data["events"] == []


def test_read_ledger_non_dict_host_entry_is_fresh(tmp_path):
    """Structurally corrupt but valid JSON: a host entry that is not a dict
    (e.g. a bare string) is treated like syntax corruption — fresh ledger, so
    a later setdefault() never sees a string/list where an entry belongs."""
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": {"web": "not-a-dict"}, "events": []}), encoding="utf-8")
    assert read_ledger(tmp_path) == {"hosts": {}, "events": []}


def test_read_ledger_non_dict_event_is_fresh(tmp_path):
    """A non-dict event in an otherwise valid ledger is treated as corrupt."""
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": {}, "events": ["junk", 42]}), encoding="utf-8")
    assert read_ledger(tmp_path) == {"hosts": {}, "events": []}


def test_read_ledger_wrong_hosts_container_is_fresh(tmp_path):
    """'hosts' holding a list instead of a dict is corrupt — fresh ledger."""
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": ["web-01"], "events": []}), encoding="utf-8")
    assert read_ledger(tmp_path) == {"hosts": {}, "events": []}


def test_read_ledger_wrong_events_container_is_fresh(tmp_path):
    """'events' holding a dict instead of a list is corrupt — fresh ledger."""
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": {}, "events": {"web-01": "2026"}}), encoding="utf-8")
    assert read_ledger(tmp_path) == {"hosts": {}, "events": []}


def test_read_ledger_non_utf8_is_fresh(tmp_path):
    """A hosts.json with invalid UTF-8 bytes (a truncated or legacy write)
    recovers as a fresh ledger — read_text raises UnicodeDecodeError, a
    ValueError subclass, so the same except-branch as corrupt JSON fires."""
    (tmp_path / "hosts.json").write_bytes(b'\xff\xfe{"hosts": {"web": {}}}')
    assert read_ledger(tmp_path) == {"hosts": {}, "events": []}


def test_read_ledger_event_missing_field_is_fresh(tmp_path):
    """A structurally valid event dict missing one required field (here: no
    'ts') is corrupt — a shape observe_scan would later crash on must never
    pass read_ledger."""
    (tmp_path / "hosts.json").write_text(
        json.dumps(
            {
                "hosts": {},
                "events": [{"type": "os-upgrade", "host": "web-01", "from": "11", "to": "12"}],
            }
        ),
        encoding="utf-8",
    )
    assert read_ledger(tmp_path) == {"hosts": {}, "events": []}


def test_read_ledger_event_non_str_field_is_fresh(tmp_path):
    """An event dict whose field value is not a string (e.g. an int ts) is
    corrupt — ledger events only ever carry str values."""
    (tmp_path / "hosts.json").write_text(
        json.dumps(
            {
                "hosts": {},
                "events": [{"type": "os-upgrade", "host": "web-01", "from": "11", "to": "12", "ts": 20260101}],
            }
        ),
        encoding="utf-8",
    )
    assert read_ledger(tmp_path) == {"hosts": {}, "events": []}


# --- run identities (per-bucket key derivation) ---------------------------- #


def test_host_key_lxc_is_node_id_not_name():
    """The deliberate PR3 adjustment: LXC identity is node/id, multi-cluster
    safe — a bare vmid is not fleet-unique — NOT the container name."""
    rec = {"node": "pve-01", "name": "sonarr", "id": "101"}
    assert _host_key("lxc", rec) == "pve-01/101"
    assert _host_key("lxc", rec) != "sonarr"
    # same id on another node is a different host
    assert _host_key("lxc", {"node": "pve-02", "id": "101"}) == "pve-02/101"


def test_host_key_per_bucket():
    assert _host_key("vm", {"node": "pve-01", "name": "media-vm", "vmid": "200"}) == "media-vm"
    assert _host_key("remote", {"host": "web-01", "status": "OK"}) == "web-01"
    assert _host_key("node", {"node": "pve-01", "status": "OK"}) == "pve-01"
    # manager self-update is a NodeRecord with node="Ansible-Manager"
    assert _host_key("node", {"node": "Ansible-Manager"}) == "Ansible-Manager"


def test_host_key_custom_excluded_and_legacy_shapes():
    assert _host_key("custom", {"host": "h", "name": "gitea"}) is None
    # legacy LXC records without id / without node are unidentifiable — skipped
    assert _host_key("lxc", {"name": "sonarr"}) is None
    assert _host_key("lxc", {"id": "101"}) is None
    assert _host_key("vm", {"vmid": "200"}) is None
    assert _host_key("remote", {"status": "OK"}) is None
    assert _host_key("node", {}) is None
    assert _host_key("unknown-bucket", {"host": "x"}) is None


# --- observe_run ------------------------------------------------------------ #


def test_observe_run_sets_last_run_and_status(tmp_path):
    observe_run(tmp_path, _run({"remote": [{"host": "web-01", "status": "OK"}]}, ts="20260101T000000000000Z"))
    entry = read_ledger(tmp_path)["hosts"]["web-01"]
    assert entry["last_run_ts"] == "20260101T000000000000Z"
    assert entry["last_status"] == "OK"
    assert "last_changed_ts" not in entry


def test_observe_run_last_changed_only_on_applied_os_update(tmp_path):
    """last_changed_ts is set only when the *OS* applied-update status matches
    the shared history._UPDATED_RE predicate — never for dry-run "WOULD UPDATE",
    FAILED/ROLLED BACK, or an LXC community-script app update alone."""
    observe_run(
        tmp_path,
        _run(
            {
                # lxc: os line is the OS status — updated → counts
                "lxc": [
                    {"node": "pve-01", "name": "a", "id": "101", "app": "OK", "os": "Updated (3 upgraded)"},
                    # app updated but OS clean → does NOT count (OS-only predicate)
                    {"node": "pve-01", "name": "b", "id": "102", "app": "Updated: v4.0 → v4.1", "os": "OK"},
                    # dry-run / failure strings do not match
                    {"node": "pve-01", "name": "c", "id": "103", "app": "OK", "os": "WOULD UPDATE"},
                    {"node": "pve-01", "name": "d", "id": "104", "app": "FAILED + ROLLED BACK", "os": "OK"},
                ],
                "vm": [
                    {"node": "pve-01", "name": "media", "vmid": "200", "status": "UPDATED & REBOOTED"},
                    {"node": "pve-01", "name": "media2", "vmid": "201", "status": "WOULD UPDATE"},
                ],
                "remote": [{"host": "web-01", "status": "UPDATED"}],
                "node": [
                    {"node": "pve-02", "status": "UPDATED (MANUAL REBOOT REQ)"},
                    {"node": "pve-03", "status": "FAILED"},
                ],
            }
        ),
    )
    hosts = read_ledger(tmp_path)["hosts"]
    assert hosts["pve-01/101"]["last_changed_ts"] == "20260101T000000000000Z"
    assert "last_changed_ts" not in hosts["pve-01/102"]  # app-only update
    assert "last_changed_ts" not in hosts["pve-01/103"]  # WOULD UPDATE
    assert "last_changed_ts" not in hosts["pve-01/104"]  # FAILED + ROLLED BACK
    assert hosts["media"]["last_changed_ts"] == "20260101T000000000000Z"
    assert "last_changed_ts" not in hosts["media2"]
    assert hosts["web-01"]["last_changed_ts"] == "20260101T000000000000Z"
    assert hosts["pve-02"]["last_changed_ts"] == "20260101T000000000000Z"
    assert "last_changed_ts" not in hosts["pve-03"]


def test_observe_run_node_dry_run_does_not_set_last_changed(tmp_path):
    """PR3: node status strings have no simulation variant (a dry run reports
    plain 'UPDATED'), so the record's dry_run flag is the only signal the
    ledger has — a dry_run=true record must not set last_changed_ts."""
    observe_run(
        tmp_path,
        _run(
            {
                "node": [
                    # simulated run: same status string, flagged dry_run → no change
                    {"node": "pve-01", "status": "UPDATED", "dry_run": True},
                    # real run: same status string, no flag → counts
                    {"node": "pve-02", "status": "UPDATED"},
                ],
            },
            ts="20260101T000000000000Z",
        ),
    )
    hosts = read_ledger(tmp_path)["hosts"]
    # last_run/last_status are still recorded so the host shows up in the UI
    assert hosts["pve-01"]["last_run_ts"] == "20260101T000000000000Z"
    assert "last_changed_ts" not in hosts["pve-01"]
    assert hosts["pve-02"]["last_changed_ts"] == "20260101T000000000000Z"


def test_observe_run_manager_dry_run_does_not_set_last_changed(tmp_path):
    """The manager self-update (NodeRecord node='Ansible-Manager') follows the
    same rule: dry_run=true + UPDATED → no last_changed_ts."""
    observe_run(
        tmp_path,
        _run(
            {
                "node": [{"node": "Ansible-Manager", "status": "UPDATED", "dry_run": True}],
            },
            ts="20260101T000000000000Z",
        ),
    )
    entry = read_ledger(tmp_path)["hosts"]["Ansible-Manager"]
    assert entry["last_run_ts"] == "20260101T000000000000Z"
    assert "last_changed_ts" not in entry


def test_observe_run_accumulates_across_runs(tmp_path):
    """A host's last_run/last_status/last_changed all move forward run by run;
    an earlier last_changed survives a later clean run."""
    observe_run(
        tmp_path,
        _run(
            {"lxc": [{"node": "pve-01", "name": "a", "id": "101", "app": "OK", "os": "Updated (1 upgraded)"}]},
            ts="20260101T000000000000Z",
        ),
    )
    observe_run(
        tmp_path,
        _run(
            {"lxc": [{"node": "pve-01", "name": "a", "id": "101", "app": "OK", "os": "OK"}]},
            ts="20260102T000000000000Z",
        ),
    )
    entry = read_ledger(tmp_path)["hosts"]["pve-01/101"]
    assert entry["last_run_ts"] == "20260102T000000000000Z"
    assert entry["last_status"] == "OK"
    # last_changed stays at the last run that actually applied an update
    assert entry["last_changed_ts"] == "20260101T000000000000Z"


def test_observe_run_custom_records_excluded(tmp_path):
    observe_run(tmp_path, _run({"custom": [{"host": "h", "name": "gitea", "app": "OK"}]}))
    assert read_ledger(tmp_path)["hosts"] == {}


def test_observe_run_empty_summary_writes_nothing(tmp_path):
    observe_run(tmp_path, _run({}))
    assert not (tmp_path / "hosts.json").exists()


def test_observe_run_corrupt_ledger_recovers(tmp_path):
    """A corrupt hosts.json must not fail observe_run — it reseeds fresh and
    the new observation lands."""
    (tmp_path / "hosts.json").write_text("{corrupt", encoding="utf-8")
    observe_run(tmp_path, _run({"remote": [{"host": "web", "status": "UPDATED"}]}))
    data = read_ledger(tmp_path)
    assert data["hosts"]["web"]["last_status"] == "UPDATED"


def test_observe_run_non_utf8_ledger_recovers(tmp_path):
    """A non-UTF-8 hosts.json must not fail observe_run — read_ledger reseeds
    fresh (UnicodeDecodeError is a ValueError) and the observation lands."""
    (tmp_path / "hosts.json").write_bytes(b"\xff\xfe")
    observe_run(tmp_path, _run({"remote": [{"host": "web", "status": "UPDATED"}]}))
    data = read_ledger(tmp_path)
    assert data["hosts"]["web"]["last_status"] == "UPDATED"


def test_observe_run_structurally_corrupt_ledger_recovers(tmp_path):
    """A valid-JSON but structurally corrupt hosts.json must not fail
    observe_run — read_ledger reseeds fresh and the observation lands."""
    (tmp_path / "hosts.json").write_text(
        json.dumps({"hosts": {"web": "garbage"}, "events": ["junk"]}), encoding="utf-8"
    )
    observe_run(tmp_path, _run({"remote": [{"host": "web", "status": "UPDATED"}]}))
    data = read_ledger(tmp_path)
    assert data["hosts"]["web"]["last_status"] == "UPDATED"
    assert data["events"] == []


def test_observe_run_skips_non_mapping_records(tmp_path):
    observe_run(tmp_path, _run({"remote": ["not-a-record", 42, None]}))
    assert read_ledger(tmp_path)["hosts"] == {}


# --- observe_scan ----------------------------------------------------------- #


def test_observe_scan_first_observation_is_baseline_no_event(tmp_path):
    """The first scan of a host stores its release and emits no event."""
    observe_scan(
        tmp_path,
        _scan(
            ts="20260101T000000000000Z",
            hosts={
                "web-01": {
                    "kind": "remote",
                    "os_release": {"id": "debian", "version_id": "11", "pretty_name": "Debian 11 (bullseye)"},
                }
            },
        ),
    )
    data = read_ledger(tmp_path)
    assert data["events"] == []
    assert data["hosts"]["web-01"]["os_release"]["version_id"] == "11"


def test_observe_scan_version_change_emits_event(tmp_path):
    observe_scan(
        tmp_path,
        _scan(
            ts="20260101T000000000000Z",
            hosts={"web-01": {"kind": "remote", "os_release": {"id": "debian", "version_id": "11"}}},
        ),
    )
    observe_scan(
        tmp_path,
        _scan(
            ts="20260102T000000000000Z",
            hosts={"web-01": {"kind": "remote", "os_release": {"id": "debian", "version_id": "12"}}},
        ),
    )
    data = read_ledger(tmp_path)
    assert data["events"] == [
        {"type": "os-upgrade", "host": "web-01", "from": "11", "to": "12", "ts": "20260102T000000000000Z"}
    ]
    assert data["hosts"]["web-01"]["os_release"]["version_id"] == "12"


def test_observe_scan_unchanged_version_no_event(tmp_path):
    observe_scan(
        tmp_path,
        _scan(
            ts="20260101T000000000000Z",
            hosts={"web-01": {"kind": "remote", "os_release": {"id": "debian", "version_id": "12"}}},
        ),
    )
    observe_scan(
        tmp_path,
        _scan(
            ts="20260102T000000000000Z",
            hosts={"web-01": {"kind": "remote", "os_release": {"id": "debian", "version_id": "12"}}},
        ),
    )
    assert read_ledger(tmp_path)["events"] == []


def test_observe_scan_version_id_preferred_over_pretty_name(tmp_path):
    """The comparison uses version_id when present — a pretty_name-only change
    that keeps the same version_id must not fire (and vice-versa the event
    carries version_id values)."""
    observe_scan(
        tmp_path,
        _scan(
            ts="20260101T000000000000Z",
            hosts={
                "web-01": {
                    "kind": "remote",
                    "os_release": {"id": "debian", "version_id": "11", "pretty_name": "Debian 11 (bullseye)"},
                }
            },
        ),
    )
    # pretty_name text changed but version_id stayed 11 → no event
    observe_scan(
        tmp_path,
        _scan(
            ts="20260102T000000000000Z",
            hosts={
                "web-01": {
                    "kind": "remote",
                    "os_release": {"id": "debian", "version_id": "11", "pretty_name": "Debian GNU/Linux 11 (bullseye)"},
                }
            },
        ),
    )
    assert read_ledger(tmp_path)["events"] == []
    # version_id moves → event carries the version_id values
    observe_scan(
        tmp_path,
        _scan(
            ts="20260103T000000000000Z",
            hosts={
                "web-01": {
                    "kind": "remote",
                    "os_release": {"id": "debian", "version_id": "12", "pretty_name": "Debian 12 (bookworm)"},
                }
            },
        ),
    )
    assert read_ledger(tmp_path)["events"][0]["from"] == "11"
    assert read_ledger(tmp_path)["events"][0]["to"] == "12"


def test_observe_scan_pretty_name_fallback(tmp_path):
    """A release without version_id falls back to pretty_name for compare+event."""
    observe_scan(
        tmp_path,
        _scan(
            ts="20260101T000000000000Z",
            hosts={"pve-01": {"kind": "node", "os_release": {"id": "alpine", "pretty_name": "Alpine 3.20"}}},
        ),
    )
    observe_scan(
        tmp_path,
        _scan(
            ts="20260102T000000000000Z",
            hosts={"pve-01": {"kind": "node", "os_release": {"id": "alpine", "pretty_name": "Alpine 3.21"}}},
        ),
    )
    data = read_ledger(tmp_path)
    assert data["events"][0]["from"] == "Alpine 3.20"
    assert data["events"][0]["to"] == "Alpine 3.21"


def test_observe_scan_lxc_entries_keyed_node_id(tmp_path):
    """Scan lxc entries are observed under their composite node/id key, and
    two clusters' same-id containers stay distinct hosts."""
    observe_scan(
        tmp_path,
        _scan(
            ts="20260101T000000000000Z",
            lxc={
                "alpha-01/101": {
                    "node": "alpha-01",
                    "id": "101",
                    "name": "a",
                    "os_release": {"id": "debian", "version_id": "12"},
                },
                "beta-01/101": {
                    "node": "beta-01",
                    "id": "101",
                    "name": "b",
                    "os_release": {"id": "debian", "version_id": "12"},
                },
            },
        ),
    )
    hosts = read_ledger(tmp_path)["hosts"]
    assert hosts["alpha-01/101"]["os_release"]["version_id"] == "12"
    assert hosts["beta-01/101"]["os_release"]["version_id"] == "12"
    assert set(hosts) == {"alpha-01/101", "beta-01/101"}


def test_observe_scan_legacy_bare_lxc_key_normalised(tmp_path):
    """Pre-PR3 snapshots key lxc entries by the bare id; observe_scan must
    normalise them to node/id using the entry's own node field."""
    observe_scan(
        tmp_path,
        _scan(
            ts="20260101T000000000000Z",
            lxc={"101": {"node": "pve-01", "name": "sonarr", "os_release": {"id": "debian", "version_id": "11"}}},
        ),
    )
    data = read_ledger(tmp_path)
    assert "pve-01/101" in data["hosts"]
    assert "101" not in data["hosts"]


def test_scan_lxc_key_helper():
    assert _scan_lxc_key("pve-01/101", {"node": "pve-01", "id": "101"}) == "pve-01/101"
    # legacy bare key: entry's own node wins
    assert _scan_lxc_key("101", {"node": "pve-01", "name": "sonarr"}) == "pve-01/101"
    # entry node/id win over a mismatch in the dict key
    assert _scan_lxc_key("old/101", {"node": "new", "id": "101"}) == "new/101"
    # a bare key with no node cannot be made multi-cluster-safe → skipped
    assert _scan_lxc_key("101", {}) is None
    assert _scan_lxc_key("101", {"id": "101"}) is None
    assert _scan_lxc_key("101", {"name": "sonarr"}) is None


def test_observe_scan_entries_without_os_release_skipped(tmp_path):
    """Error/unreachable/legacy entries lacking a usable os_release are ignored
    — they create no ledger host and no event."""
    observe_scan(
        tmp_path,
        _scan(
            ts="20260101T000000000000Z",
            hosts={
                "down-01": {
                    "kind": "remote",
                    "error": "unreachable",
                    "os_release": {"id": "", "version_id": "", "pretty_name": ""},
                }
            },
            lxc={"pve-01/101": {"node": "pve-01", "id": "101", "error": "boom"}},
        ),
    )
    assert read_ledger(tmp_path)["hosts"] == {}


def test_observe_scan_corrupt_ledger_recovers(tmp_path):
    (tmp_path / "hosts.json").write_text("[1,2,3]", encoding="utf-8")
    observe_scan(
        tmp_path,
        _scan(
            ts="20260101T000000000000Z",
            hosts={"web-01": {"kind": "remote", "os_release": {"id": "debian", "version_id": "12"}}},
        ),
    )
    assert read_ledger(tmp_path)["hosts"]["web-01"]["os_release"]["version_id"] == "12"


def test_observe_scan_structurally_corrupt_ledger_recovers(tmp_path):
    """Same recovery for observe_scan: a valid-JSON ledger with wrong
    hosts/events containers reseeds fresh and the scan observation lands."""
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": ["oops"], "events": {}}), encoding="utf-8")
    observe_scan(
        tmp_path,
        _scan(
            ts="20260101T000000000000Z",
            hosts={"web-01": {"kind": "remote", "os_release": {"id": "debian", "version_id": "12"}}},
        ),
    )
    data = read_ledger(tmp_path)
    assert data["hosts"]["web-01"]["os_release"]["version_id"] == "12"
    assert data["events"] == []


# --- events cap ------------------------------------------------------------- #


def test_observe_scan_events_capped_at_newest_100(tmp_path):
    """Only the newest 100 events are kept; the oldest are dropped on append."""
    total = _EVENTS_CAP + 25
    # Baseline every host first (first observation emits no event), then move
    # each one to a new version so every subsequent scan fires an event.
    observe_scan(
        tmp_path,
        _scan(
            ts="202601000T000000000000Z",
            hosts={
                f"web-{i}": {"kind": "remote", "os_release": {"id": "debian", "version_id": "0"}} for i in range(total)
            },
        ),
    )
    for i in range(total):
        observe_scan(
            tmp_path,
            _scan(
                ts=f"202601{i:03d}T000000000000Z",
                hosts={f"web-{i}": {"kind": "remote", "os_release": {"id": "debian", "version_id": str(i + 1)}}},
            ),
        )
    data = read_ledger(tmp_path)
    events = data["events"]
    assert len(events) == _EVENTS_CAP
    # newest first: first event is the last scan
    assert events[0]["host"] == f"web-{total - 1}"
    assert events[0]["ts"] == f"202601{total - 1:03d}T000000000000Z"
    # the dropped window is the *oldest* scans (web-0 … web-24 dropped)
    assert all(e["host"] != "web-0" for e in events)
    assert events[-1]["host"] == f"web-{total - _EVENTS_CAP}"


# --- durability: pruning never loses ledger state --------------------------- #


def test_ledger_survives_run_pruning(tmp_path):
    """write_history prunes run files to keep=N; the ledger keeps every host
    observed, including ones whose run files are long gone."""
    for i, host in enumerate(("web-01", "web-02", "web-03")):
        state = FleetState.from_raw({"fleet_remote_data": [{"host": host, "status": "UPDATED", "pkg_count": 1}]})
        write_history(state, history_dir=tmp_path, keep=1, timestamp=f"2026010{i}T000000000000Z")
    runs = [p.name for p in tmp_path.glob("run-*.json")]
    assert runs == ["run-20260102T000000000000Z.json"]  # pruned to newest 1
    hosts = read_ledger(tmp_path)["hosts"]
    assert set(hosts) == {"web-01", "web-02", "web-03"}
    assert all(
        hosts[h]["last_changed_ts"] == f"2026010{i}T000000000000Z" for i, h in enumerate(("web-01", "web-02", "web-03"))
    )


def test_ledger_survives_scan_pruning(tmp_path):
    """write_pending prunes pending snapshots to keep=N; the ledger keeps the
    baseline+event state of every observed host."""
    from proxmox_fleet import scan as scan_mod

    for i in range(3):
        scan_mod.write_pending(
            _scan(
                ts=f"2026010{i}T000000000000Z",
                hosts={f"web-{i}": {"kind": "remote", "os_release": {"id": "debian", "version_id": str(i)}}},
            ),
            history_dir=tmp_path,
            keep=1,
        )
    files = [p.name for p in tmp_path.glob("pending-*.json") if p.name != "pending-latest.json"]
    assert files == ["pending-20260102T000000000000Z.json"]
    hosts = read_ledger(tmp_path)["hosts"]
    assert set(hosts) == {"web-0", "web-1", "web-2"}


# --- write serialization failure never fails the observation ---------------- #


def _raise_type_error(*_args, **_kwargs):
    raise TypeError("not JSON serializable")


def test_observe_run_write_serialization_failure_does_not_raise(monkeypatch, tmp_path):
    """A json.dumps TypeError while persisting hosts.json must not fail the
    run — observe_run swallows write errors (auxiliary accumulator)."""
    monkeypatch.setattr(json, "dumps", _raise_type_error)
    observe_run(tmp_path, _run({"remote": [{"host": "web", "status": "UPDATED"}]}))


def test_observe_scan_write_serialization_failure_does_not_raise(monkeypatch, tmp_path):
    """Same guarantee for observe_scan: an unserialisable ledger never fails
    the scan."""
    monkeypatch.setattr(json, "dumps", _raise_type_error)
    observe_scan(
        tmp_path,
        _scan(
            ts="20260101T000000000000Z",
            hosts={"web-01": {"kind": "remote", "os_release": {"id": "debian", "version_id": "12"}}},
        ),
    )
