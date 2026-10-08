"""Retention decisions against durable coverage, actual files and stored HTTP streams."""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import List

import pytest

from proxmox_fleet import housekeeping, housekeeping_io as io
from proxmox_fleet import housekeeping_native as native
from proxmox_fleet import housekeeping_retention as retention
from proxmox_fleet.alloy import DesiredAlloyConfig
from proxmox_fleet.housekeeping_checkpoint import CheckpointStore, GuestKey, PruneIntent, SourceIdentity
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult

KEY = GuestKey("cluster", "node", "123")
NOW = 1_800_000_000_000_000_000
DAY = 86_400_000_000_000
TASK = "/var/log/proxmox-backup/tasks/AB/UPID:node:00000001:00000002:00000003:verify:root@pam:"
NATIVE = """# unrelated policy must survive byte-for-byte
/var/log/unrelated.log {
    weekly
    rotate 4
}
/data/logs/*_access.log /data/logs/*/access.log {
    create 0644
    weekly
    rotate 4
    compress
    sharedscripts
    postrotate
        if test -f /run/nginx/nginx.pid; then { kill -USR1 $(cat /run/nginx/nginx.pid); }; fi
    endscript
}
/data/logs/*_error.log /data/logs/*/error.log {
    create 0644
    weekly
    rotate 10
    compress
    sharedscripts
    postrotate
        kill -USR1 $(cat /run/nginx/nginx.pid)
    endscript
}
/data/logs/backend.log {
    size 10M
    rotate 5
    compress
    copytruncate
}
"""


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class Guest:
    """Execution adapter: real policy/native reads and writes; no real services."""

    def __init__(self, root: Path, profiles=("npm",)):
        self.root = root
        self.profiles = set(profiles)
        self.access = True
        self.running = True
        self.journal_bytes = 0
        self.vacuum_bytes = None
        self.journal: List[str] = []
        self.native_stdout: List[str] = []
        self.pruned: List[List[str]] = []
        self.prune_failure = False
        self.probe_failure_after_write = False
        self.acknowledge_without_write = False
        self.pbs_path_recorded = True
        self.pbs_ua_recorded = True
        self.alloy_active = True
        self.before_probe = None
        self.after_prune = None
        self.put(io.POLICY_FILES["alloy_env"], housekeeping.journal_env_content(48))
        self.put(native.NPM_LOGROTATE_ORIGINAL, NATIVE)
        self.put("/proc/1/cmdline", "init\0")
        (root / "proc/1/fd").mkdir()
        (root / "proc/1/fdinfo").mkdir()
        if "npm" in self.profiles:
            self.path("/data/logs").mkdir(parents=True)
        if "pbs" in self.profiles:
            self.put(retention.PBS_API_ACCESS_LOG, "")

    def path(self, name):
        return self.root / name.lstrip("/")

    def put(self, name, value, *, age=0):
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        os.utime(path, ns=(NOW - age, NOW - age))
        return path

    def wire(self, name):
        info = self.path(name).stat()
        profile = "npm" if name.startswith("/data/") else "pbs"
        kind = "application" if profile == "npm" else ("task" if "/tasks/" in name else "api")
        if name.endswith("/archive"):
            kind = "task_index"
        return dict(path=name, device=info.st_dev, inode=info.st_ino, size=info.st_size,
                    mtime_ns=info.st_mtime_ns, allocated_bytes=info.st_blocks * 512,
                    compression="plain", profile=profile, log_kind=kind, is_active=False)

    def files(self):
        result = []
        for root in ("/data/logs", "/var/log/proxmox-backup"):
            for path in self.path(root).rglob("*"):
                if path.is_file() and ".fleet-housekeeping-quarantine" not in path.parts:
                    result.append(self.wire("/" + str(path.relative_to(self.root))))
        return result

    def housekeeping_probe(self, lxc_id):
        if self.before_probe:
            self.before_probe(self)
        if self.probe_failure_after_write and self.path(native.JOURNALD_DROPIN).exists():
            return PrimitiveResult(rc=1, failed=True)
        hashes = {}
        for name, path in io.POLICY_FILES.items():
            candidate = self.path(path)
            hashes[name] = sha(candidate.read_bytes()) if candidate.exists() else ""
        evidence = {
            "npm": {key: "npm" in self.profiles for key in
                    ("npm.service", "openresty.service", "app_root", "log_root", "detected")},
            "pbs": {key: "pbs" in self.profiles for key in
                    ("proxmox-backup-manager", "proxmox-backup-proxy.service", "task_root", "api_root", "detected")},
        }
        binaries = {name: "/usr/bin/" + name for name in housekeeping.REQUIRED_BINARIES}
        facts = dict(guest=dict(name="guest", os_type="debian", is_running=self.running, is_template=False),
                     disk=dict(total_bytes=100, available_bytes=50, used_percent=50.0),
                     journal_bytes=self.journal_bytes, profiles=sorted(self.profiles), files=self.files(),
                     cache_paths={}, busy_tools=[], binaries=binaries,
                     log_access_ready=self.access, log_access_detail=dict(alloy_user=True, dirs={}, files={}),
                     policy_sha256=hashes, profile_evidence=evidence)
        return PrimitiveResult(rc=0, facts=facts)

    def housekeeping_apply(self, lxc_id, *, command):
        parts = shlex.split(command)
        if parts[:2] == ["python3", "-c"]:
            program = parts[2]
            if program == retention._PBS_PROBE_PROGRAM:
                text = "GET " + urllib.parse.urlsplit(parts[3]).path if self.pbs_path_recorded else "GET /"
                if self.pbs_ua_recorded:
                    text += " " + parts[4]
                with self.path(retention.PBS_API_ACCESS_LOG).open("a") as handle:
                    handle.write(text + "\n")
                return PrimitiveResult(rc=0)
            if program == native._WRITE_PROGRAM and self.acknowledge_without_write:
                return PrimitiveResult(rc=0)
            mapped = [sys.executable, "-c", program, str(self.path(parts[3])), *parts[4:]]
            result = subprocess.run(mapped, capture_output=True, text=True, check=False)
            if program == retention._NATIVE_LOG_PROGRAM:
                self.native_stdout.append(result.stdout)
            return PrimitiveResult(rc=result.returncode, stdout=result.stdout, stderr=result.stderr)
        if parts[0] == "install":
            self.path(parts[-1]).mkdir(parents=True, exist_ok=True)
        elif parts[0] == "logger":
            self.journal.append(parts[-1])
        elif parts[:2] == ["rm", "-f"]:
            self.path(parts[-1]).unlink(missing_ok=True)
        elif parts[0] == "journalctl" and any("vacuum" in arg for arg in parts):
            if self.vacuum_bytes is not None:
                self.journal_bytes = self.vacuum_bytes
        return PrimitiveResult(rc=0)

    def housekeeping_prune(self, lxc_id, *, files):
        self.pruned.append([item["path"] for item in files])
        if self.prune_failure:
            return PrimitiveResult(rc=1, failed=True)
        result = io.prune({"files": files}, sysroot=str(self.root))
        if self.after_prune:
            self.after_prune(self)
        return PrimitiveResult(rc=0, facts=result)

    def alloy_probe(self, *, lxc_id=None):
        configuration = self.path("/etc/alloy/config.alloy")
        return PrimitiveResult(rc=0, facts=dict(binary_present=True, package_manager="apt",
            config_sha256=sha(configuration.read_bytes()) if configuration.exists() else "",
            journal_member=True, service_enabled=True, service_active=self.alloy_active))


class Loki:
    """Queries filter independently collected records, never manufacture query tokens."""

    def __init__(self, guest):
        self.guest = guest
        self.ready = True
        self.deliver = True
        self.wrong_task = False
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/ready":
                    self.send_response(200 if owner.ready else 503)
                    self.end_headers()
                    return
                params = urllib.parse.parse_qs(parsed.query)
                query = params["query"][0]
                result = owner.query(query)
                raw = json.dumps(dict(status="success", data=dict(resultType="streams", result=result))).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def query(self, query):
        if not self.deliver:
            return []
        selector, pipeline = query.split("}", 1)
        pairs = re.findall(r'(\w+)=("(?:\\.|[^"\\])*")', selector)
        wanted = {key: json.loads(value) for key, value in pairs}
        filename_match = re.search(r'filename=("(?:\\.|[^"\\])*")', pipeline)
        filename = json.loads(filename_match.group(1)) if filename_match else None
        token_match = re.search(r'\|=\s*("(?:\\.|[^"\\])*")', pipeline)
        token = json.loads(token_match.group(1)) if token_match else None
        records = [(dict(job="systemd-journal", role="guest", host="guest"), line) for line in self.guest.journal]
        for wire in self.guest.files():
            path = wire["path"]
            if wire["profile"] == "npm" and not path.endswith(".log"):
                continue
            if wire["log_kind"] == "task_index":
                continue
            labels = dict(job="lxc-file", delivery="live", cluster=KEY.cluster, node=KEY.node,
                          guest_id=KEY.lxc_id, host="guest", app="nginxproxymanager" if wire["profile"] == "npm" else "proxmox-backup",
                          log_kind=wire["log_kind"])
            for line in self.guest.path(path).read_text().split("\n")[:-1]:
                if self.wrong_task and wire["log_kind"] == "task":
                    line = "a different task line"
                records.append((labels, json.dumps(dict(filename=path, _entry=line))))
        result = []
        for labels, entry in records:
            if any(labels.get(key) != value for key, value in wanted.items()):
                continue
            extracted = dict(labels)
            if "| unpack" in pipeline:
                packed = json.loads(entry)
                extracted["filename"] = packed["filename"]
                entry = packed["_entry"]
            if filename is not None and extracted.get("filename") != filename:
                continue
            if token is not None and token not in entry:
                continue
            result.append(dict(stream=extracted, values=[[str(NOW), entry]]))
        return result

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def guest(tmp_path):
    root = tmp_path / "guest"
    root.mkdir()
    return Guest(root)


def setup(store, guest, name="/data/logs/proxy_access.log.1", *, acknowledge=True):
    guest.put(name, "old retained content\n", age=10 * DAY)
    wire = guest.wire(name)
    record = SourceIdentity(**wire)
    store.record_source(KEY, name, record, in_initial_manifest=True,
                        provenance={"complete": acknowledge, "complete_size": wire["size"]})
    digest = sha(guest.path(name).read_bytes())
    store.set_source_digest(KEY, name, digest)
    if acknowledge:
        store.mark_source_acknowledged(KEY, name)
    return {**wire, "source_id": name, "sha256": digest}


def run(guest, store, loki, covered=(), *, dry_run=False):
    settings = GlobalSettings(housekeeping_enabled=True, housekeeping_loki_url=loki.url)
    content = ('loki.write "default" { endpoint { url = "' + loki.url + '/loki/api/v1/push" } }\n'
               'loki.source.journal "journal" { max_age = coalesce(sys.env("FLEET_JOURNAL_MAX_AGE"), "48h") }\n')
    base = DesiredAlloyConfig(content=content, sha256=sha(content.encode()))
    effective = housekeeping.render_lxc_log_config(base, node=KEY.node, cluster=KEY.cluster,
        lxc_id=KEY.lxc_id, name="guest", profiles=guest.profiles, retention_hours=48)
    if not guest.path("/etc/alloy/config.alloy").exists() and not dry_run:
        guest.put("/etc/alloy/config.alloy", effective.content)
    probe = housekeeping.probe_housekeeping(guest, KEY.lxc_id)
    return retention.apply_guest_retention(guest, settings, store, KEY, probe, effective,
        covered_files=list(covered), dry_run=dry_run, sleep=lambda _: None, clock_ns=lambda: NOW)


@pytest.fixture
def store(tmp_path):
    with CheckpointStore.open(tmp_path / "checkpoint.sqlite3") as value:
        value.begin_capture(KEY, "capture", [], [])
        value.finish_capture(KEY, "capture", state="complete")
        yield value


def test_native_cutover_preserves_three_writer_mechanisms_and_unrelated_policy(guest, store):
    with Loki(guest) as loki:
        result = run(guest, store, loki)
    assert not result.failed
    assert guest.path(native.NPM_LOGROTATE_BACKUP).read_text() == NATIVE
    remaining = guest.path(native.NPM_LOGROTATE_ORIGINAL).read_text()
    assert remaining == NATIVE[:NATIVE.index("/data/logs/")]
    parsed = native.parse_logrotate(guest.path(native.NPM_LOGROTATE_MANAGED).read_text())
    assert parsed.ambiguous is None
    assert [block.header for block in parsed.npm_blocks] == [
        ("/data/logs/*_access.log", "/data/logs/*/access.log"),
        ("/data/logs/*_error.log", "/data/logs/*/error.log"), ("/data/logs/backend.log",)]
    access, error, backend = parsed.npm_blocks
    assert "copytruncate" not in "\n".join(access.body + error.body)
    assert "copytruncate" in "\n".join(backend.body)
    assert "{ kill -USR1" in "\n".join(access.body)
    assert all("rotate -1" in "\n".join(block.body) for block in parsed.npm_blocks)


@pytest.mark.parametrize("raw_policy", ["/data/logs/a.log /var/log/unrelated.log {\n rotate 4\n}\n",
    "/data/logs/a.log {\n postrotate\n echo bad\n", "/data/logs/../secret.log {\n daily\n}\n"])
def test_ambiguous_policy_cannot_authorize_deletion(guest, store, raw_policy):
    coverage = setup(store, guest)
    guest.put(native.NPM_LOGROTATE_ORIGINAL, raw_policy)
    with Loki(guest) as loki:
        result = run(guest, store, loki, [coverage])
    assert result.failed
    assert guest.path(coverage["path"]).exists()
    assert not guest.pruned
    assert guest.path(native.NPM_LOGROTATE_ORIGINAL).read_text() == raw_policy


@pytest.mark.parametrize("fault", ["initial", "delivery", "outage", "permissions", "postprobe", "noop_write", "durable_coverage"])
def test_deletion_is_fail_closed_at_each_authorization_seam(guest, store, fault):
    coverage = setup(store, guest, acknowledge=fault != "initial")
    with Loki(guest) as loki:
        if fault == "delivery":
            loki.deliver = False
        elif fault == "outage":
            loki.ready = False
        elif fault == "permissions":
            guest.access = False
        elif fault == "postprobe":
            guest.probe_failure_after_write = True
        elif fault == "noop_write":
            guest.acknowledge_without_write = True
        elif fault == "durable_coverage":
            store.set_source_digest(KEY, coverage["source_id"], "f" * 64)
        result = run(guest, store, loki, [coverage])
    assert guest.path(coverage["path"]).read_text() == "old retained content\n"
    assert not guest.pruned
    if fault != "initial":
        assert result.failed


def test_closed_acked_file_deleted_but_current_and_recent_files_preserved(guest, store):
    coverage = setup(store, guest)
    guest.put("/data/logs/proxy_access.log", "live data\n", age=10 * DAY)
    guest.put("/data/logs/proxy_access.log.2", "recent data\n")
    before = guest.path(coverage["path"]).stat().st_blocks * 512
    with Loki(guest) as loki:
        result = run(guest, store, loki, [coverage])
    assert not result.failed
    assert not guest.path(coverage["path"]).exists()
    assert guest.path("/data/logs/proxy_access.log").read_text() == "live data\n"
    assert guest.path("/data/logs/proxy_access.log.2").read_text() == "recent data\n"
    assert result.files_pruned == 1 and result.bytes_reclaimed == before


def test_replacement_identity_is_not_deleted(guest, store):
    coverage = setup(store, guest)
    guest.path(coverage["path"]).rename(guest.path("/data/logs/other.log.1"))
    guest.put(coverage["path"], "replacement\n", age=10 * DAY)
    with Loki(guest) as loki:
        result = run(guest, store, loki, [coverage])
    assert not result.failed
    assert result.files_pruned == 0
    assert guest.path(coverage["path"]).read_text() == "replacement\n"


def test_configuration_drift_is_reconciled_and_delivery_reverified(guest, store):
    with Loki(guest) as loki:
        assert not run(guest, store, loki).failed
        path = guest.path(native.NPM_LOGROTATE_MANAGED)
        path.write_text(path.read_text().replace("rotate -1", "rotate 4\n    maxage 2"))
        loki.deliver = False
        blocked = run(guest, store, loki)
        assert blocked.failed
        loki.deliver = True
        assert not run(guest, store, loki).failed
    assert "maxage" not in path.read_text()
    assert "rotate 4" not in path.read_text()
    verification = store.delivery_verification(KEY)
    assert verification.policy_hashes["npm_logrotate"] == sha(path.read_bytes())


def test_real_journal_allocation_not_command_stdout_drives_reclaimed_metric(guest, store):
    guest.journal_bytes = 8 * 1024 * 1024
    guest.vacuum_bytes = 3 * 1024 * 1024
    with Loki(guest) as loki:
        result = run(guest, store, loki)
    assert not result.failed
    assert result.bytes_reclaimed == 5 * 1024 * 1024


def test_prune_transport_crash_leaves_durable_intent_for_resume(guest, store):
    coverage = setup(store, guest)
    guest.prune_failure = True
    with Loki(guest) as loki:
        result = run(guest, store, loki, [coverage])
        assert result.failed
        intent = store.open_prune_intents(KEY)[0]
        assert intent.source_id == coverage["source_id"]
        assert guest.path(coverage["path"]).exists()
        guest.prune_failure = False
        resumed = run(guest, store, loki)
    assert not resumed.failed
    assert resumed.files_pruned == 1
    assert not guest.path(coverage["path"]).exists()
    assert not store.open_prune_intents(KEY)


def test_unowned_recovery_is_blocked_without_inferred_archive_success(guest, store):
    coverage = setup(store, guest)
    store.record_prune_intent(PruneIntent(intent_id="lost-owner", key=KEY, path=coverage["path"],
        quarantine_path=retention.quarantine_path_for(coverage["path"]), device=coverage["device"],
        inode=coverage["inode"], size=coverage["size"], mtime_ns=coverage["mtime_ns"], digest=coverage["sha256"]))
    with Loki(guest) as loki:
        result = run(guest, store, loki)
    assert result.failed
    assert guest.path(coverage["path"]).exists()
    assert len(store.open_prune_intents(KEY)) == 1


def test_dry_run_leaves_files_and_checkpoint_unchanged(guest, store):
    coverage = setup(store, guest)
    before = {path: path.read_bytes() for path in guest.root.rglob("*") if path.is_file()}
    with Loki(guest) as loki:
        result = run(guest, store, loki, [coverage], dry_run=True)
    assert result.findings
    assert before == {path: path.read_bytes() for path in guest.root.rglob("*") if path.is_file()}
    assert store.delivery_verification(KEY) is None
    assert not store.open_prune_intents(KEY)


@pytest.mark.parametrize("failure", [None, "missing", "old", "mismatch", "no_native_api"])
def test_pbs_requires_native_api_and_genuine_task_delivery_without_log_stdout(tmp_path, store, failure):
    root = tmp_path / "pbs"
    root.mkdir()
    guest = Guest(root, profiles=("pbs",))
    if failure != "missing":
        guest.put(TASK, "genuine private task content\n", age=10 * DAY if failure == "old" else DAY)
    if failure == "no_native_api":
        guest.pbs_path_recorded = guest.pbs_ua_recorded = False
    with Loki(guest) as loki:
        loki.wrong_task = failure == "mismatch"
        result = run(guest, store, loki)
    assert result.failed is (failure is not None)
    assert all("genuine private task content" not in value for value in guest.native_stdout)
    assert guest.path(TASK).exists() is (failure != "missing")
    if failure is not None:
        assert not guest.path(native.JOURNALD_DROPIN).exists()


def test_owned_policy_symlink_never_overwrites_unrelated_target(guest, store, tmp_path):
    target = tmp_path / "unrelated"
    target.write_text("preserve me")
    owned = guest.path(native.JOURNALD_DROPIN)
    owned.parent.mkdir(parents=True, exist_ok=True)
    owned.symlink_to(target)
    with Loki(guest) as loki:
        result = run(guest, store, loki)
    assert result.failed
    assert target.read_text() == "preserve me"
    assert owned.is_symlink()


@pytest.mark.parametrize("fault", ["outage", "policy", "permissions", "alloy_service", "alloy_config"])
def test_mid_prune_drift_preserves_remaining_archives_and_measured_progress(guest, store, fault):
    covered = [setup(store, guest, f"/data/logs/proxy{index}_access.log.1") for index in range(150)]
    with Loki(guest) as loki:
        def interrupt(current):
            if fault == "outage":
                loki.ready = False
            elif fault == "policy":
                current.put(native.JOURNALD_DROPIN, "[Journal]\nSystemMaxUse=1M\n")
            elif fault == "alloy_service":
                current.alloy_active = False
            elif fault == "alloy_config":
                current.put("/etc/alloy/config.alloy", "unverified external configuration")
            else:
                current.access = False
        guest.after_prune = interrupt
        result = run(guest, store, loki, covered)
    assert result.failed
    deleted = {item["path"] for item in covered if not guest.path(item["path"]).exists()}
    assert deleted == set(guest.pruned[0])
    assert result.files_pruned == len(deleted)
    assert result.bytes_reclaimed == sum(item["allocated_bytes"] for item in covered if item["path"] in deleted)
    for item in covered:
        if item["path"] not in deleted:
            assert guest.path(item["path"]).read_text() == "old retained content\n"
