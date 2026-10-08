"""Real-filesystem + real-HTTP regressions for the acknowledged log importer.

Every test drives ``proxmox_fleet.housekeeping_import`` end to end against:

* the real :mod:`proxmox_fleet.housekeeping_stream` readers,
* the real :mod:`proxmox_fleet.housekeeping_io` capture/snapshot helper on a
  temporary sysroot and spool,
* a real :class:`CheckpointStore` SQLite database, and
* a local HTTP server that records the exact pushed bodies.
"""
from __future__ import annotations

import gzip
import http.server
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import zstandard

from proxmox_fleet import housekeeping_import as hi
from proxmox_fleet import housekeeping_io as hio
from proxmox_fleet import housekeeping_sources as sources
from proxmox_fleet.housekeeping_checkpoint import CheckpointStore, GuestKey
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult

KEY = GuestKey("cluster-a", "node-1", "120")


# --------------------------------------------------------------------------- #
# HTTP capture server
# --------------------------------------------------------------------------- #


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        server = self.server
        server.requests.append((self.path, body, dict(self.headers)))
        if server.script:
            status, retry_after = server.script.pop(0)
        else:
            status, retry_after = server.default, None
        self.send_response(status)
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


class LokiServer:
    def __init__(self, script: Optional[List[Tuple[int, Optional[int]]]] = None, default: int = 204) -> None:
        self.requests: List[Tuple[str, bytes, Dict[str, str]]] = []
        self.script = list(script or [])
        self.default = default
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.requests = self.requests
        self.httpd.script = self.script
        self.httpd.default = self.default
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def bodies(self) -> List[bytes]:
        return [body for _path, body, _headers in self.requests]

    def __enter__(self) -> "LokiServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- #
# Guest tree / executor / settings
# --------------------------------------------------------------------------- #


def _write(root: Path, path: str, data: bytes, mode: int = 0o644) -> Path:
    target = root / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(mode)
    return target


def _wire(
    root: Path,
    path: str,
    *,
    compression: str = "plain",
    profile: str = "npm",
    log_kind: str = "application",
    is_active: bool = False,
) -> Dict[str, Any]:
    st = os.stat(root / path.lstrip("/"))
    return {
        "path": path,
        "device": int(st.st_dev),
        "inode": int(st.st_ino),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "allocated_bytes": int(st.st_blocks) * 512,
        "compression": compression,
        "profile": profile,
        "log_kind": log_kind,
        "is_active": is_active,
    }


def _ok(facts: Dict[str, Any]) -> PrimitiveResult:
    return PrimitiveResult(rc=0, changed=True, failed=False, facts=facts)


def _failed(message: str) -> PrimitiveResult:
    return PrimitiveResult(rc=1, changed=False, failed=True, stderr=message, facts={})


class FakeExecutor:
    """Node transport backed by the real housekeeping_io helper on temp paths."""

    def __init__(self, root: Path, spool: Path) -> None:
        self.root = str(root)
        self.spool = str(spool)
        self.capture_calls = 0
        self.snapshot_calls = 0
        self.release_calls = 0
        self.fail_capture = False
        self.capture_hook = None
        self.after_capture = None

    def housekeeping_capture(self, lxc_id: str, *, files, capture_id: str) -> PrimitiveResult:
        self.capture_calls += 1
        if self.capture_hook is not None:
            self.capture_hook()
        if self.fail_capture:
            return _failed("capture refused")
        try:
            facts = hio.capture(
                {"capture_id": capture_id, "files": files},
                sysroot=self.root,
                spool_root=self.spool,
            )
        except hio.HelperError as exc:
            return _failed(str(exc))
        if self.after_capture is not None:
            self.after_capture()
        return _ok(facts)

    def housekeeping_snapshot(self, lxc_id: str, *, files, destination: str) -> PrimitiveResult:
        self.snapshot_calls += 1
        try:
            facts = hio.snapshot(
                {"files": files}, destination=destination, sysroot=self.root, spool_root=self.spool
            )
        except hio.HelperError as exc:
            return _failed(str(exc))
        return _ok(facts)

    def run_shell(self, command: str, **opts: Any) -> PrimitiveResult:
        self.release_calls += 1
        proc = subprocess.run(command, shell=True, capture_output=True, text=True)
        return PrimitiveResult(
            rc=proc.returncode,
            changed=proc.returncode == 0,
            failed=proc.returncode != 0,
            stdout=proc.stdout,
            stderr=proc.stderr,
            facts={},
        )


def _settings(url: str, *, budget_mb: int = 1024) -> GlobalSettings:
    return GlobalSettings(
        housekeeping_enabled=True,
        housekeeping_loki_url=url,
        housekeeping_backfill_budget_mb=budget_mb,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    guest = tmp_path / "guest"
    (guest / "data" / "logs").mkdir(parents=True)
    spool = tmp_path / "spool"
    monkeypatch.setattr(hi, "SPOOL_ROOT", str(spool))
    executor = FakeExecutor(guest, spool)
    checkpoint_dir = tmp_path / "checkpoint"
    checkpoint_dir.mkdir()
    store = CheckpointStore.open(str(checkpoint_dir / "housekeeping.sqlite3"))
    yield guest, spool, executor, store
    store.close()


# --------------------------------------------------------------------------- #
# Entry reconstruction helper
# --------------------------------------------------------------------------- #


def _iter_entries(server: LokiServer):
    for _path, body, _headers in server.requests:
        data = json.loads(body.decode("utf-8"))
        for stream in data["streams"]:
            for _ts, line in stream["values"]:
                yield json.loads(line)


def _reconstruct(entries) -> bytes:
    out = bytearray()
    for entry in entries:
        if "raw_base64" in entry:
            import base64

            payload = base64.b64decode(entry["raw_base64"])
        else:
            payload = entry["_entry"].encode("utf-8")
        out += payload
        if entry.get("fragment_final") and entry.get("terminated"):
            out += b"\n"
    return bytes(out)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_plain_and_gzip_import_then_no_replay(env, tmp_path):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\nbeta\n")
    _write(guest, "/data/logs/access.log.2.gz", gzip.compress(b"gamma\ndelta\n"))
    wires = [
        _wire(guest, "/data/logs/access.log.1"),
        _wire(guest, "/data/logs/access.log.2.gz", compression="gzip"),
    ]
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=wires, sleep=lambda _s: None,
        )
    assert result.failed is False
    assert result.pending is False
    assert executor.capture_calls == 1
    assert server.requests and all(path.endswith(hi.LOKI_PUSH_PATH) for path, _b, _h in server.requests)
    first_push_count = len(server.requests)
    entries = list(_iter_entries(server))
    assert {entry["filename"] for entry in entries} == {"/data/logs/access.log.1", "/data/logs/access.log.2.gz"}
    assert all(entry["source_id"] for entry in entries)
    # Both closed files are covered (raw digest + identity verified, not active).
    covered_paths = {entry["path"] for entry in result.covered_files}
    assert covered_paths == {"/data/logs/access.log.1", "/data/logs/access.log.2.gz"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) for entry in result.covered_files)

    with LokiServer() as second:
        again = hi.import_guest_logs(
            executor, _settings(second.url), store, KEY,
            name="npm-ct", files=wires, sleep=lambda _s: None,
        )
    assert again.failed is False
    assert second.requests == []  # nothing new to import
    assert executor.capture_calls == 1  # capture already complete
    assert first_push_count > 0


def test_capture_failure_blocks_before_any_push(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\n")
    executor.fail_capture = True
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
            sleep=lambda _s: None,
        )
    assert result.failed is True
    assert server.requests == []
    # The frozen intent is retained so a later run resumes the same capture.
    intent = store.capture_intent(KEY)
    assert intent is not None and intent.state == "pending"


def test_capture_intent_committed_before_capture(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\n")
    seen: List[bool] = []

    def hook() -> None:
        intent = store.capture_intent(KEY)
        seen.append(intent is not None and intent.state == "pending")

    executor.capture_hook = hook
    with LokiServer() as server:
        hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
            sleep=lambda _s: None,
        )
    assert seen == [True]


def test_503_then_204_resumes_exact_body_and_gates_coverage(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\nbeta\ngamma\n")
    wire = _wire(guest, "/data/logs/access.log.1")
    with LokiServer(script=[(503, None), (503, None), (503, None)]) as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert first.pending is True
    assert first.failed is True
    assert first.covered_files == []
    assert len(server.requests) == 3  # 3 attempts, then pending
    pending = store.pending_batch(KEY)
    assert pending is not None
    saved_body = pending.body
    # Not acknowledged -> the manifest gate still reports work remaining.
    assert store.initial_manifest_state(KEY).acknowledged == 0

    with LokiServer() as server2:
        second = hi.import_guest_logs(
            executor, _settings(server2.url), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert second.failed is False
    assert second.pending is False
    assert server2.requests and server2.requests[0][1] == saved_body  # exact body resumed
    assert store.pending_batch(KEY) is None
    assert store.initial_manifest_state(KEY).acknowledged == 1
    assert {entry["path"] for entry in second.covered_files} == {"/data/logs/access.log.1"}


def test_immediate_4xx_blocks_without_disarding_bytes(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\n")
    wire = _wire(guest, "/data/logs/access.log.1")
    with LokiServer(script=[(400, None)], default=400) as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert result.failed is True
    assert len(server.requests) == 1  # no retry on a non-retryable 4xx
    assert store.pending_batch(KEY) is not None  # bytes retained
    assert store.initial_manifest_state(KEY).acknowledged == 0


def test_budget_stops_pending_then_resumes(env):
    guest, _spool, executor, store = env
    payload = b"".join(f"line-{i:06d}-{'x' * 80}\n".encode() for i in range(20000))
    _write(guest, "/data/logs/access.log.1", payload)
    wire = _wire(guest, "/data/logs/access.log.1")
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url, budget_mb=1), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert result.pending is True
    assert result.failed is False
    assert result.bytes_archived <= 1024 * 1024
    source = None
    for record in store.sources(KEY):
        if record.path == "/data/logs/access.log.1":
            source = record
    assert source is not None and source.decoded_offset < len(payload)
    assert result.covered_files == []
    assert store.initial_manifest_state(KEY).remaining == 1
    assert source.acknowledged is False
    assert sum(len(body) for body in server.bodies) <= 1024 * 1024

    # A later run with a large budget finishes the file.
    with LokiServer() as server2:
        done = hi.import_guest_logs(
            executor, _settings(server2.url, budget_mb=1024), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert done.pending is False and done.failed is False
    final = next(record for record in store.sources(KEY) if record.path == "/data/logs/access.log.1")
    assert final.decoded_offset == len(payload)
    assert {entry["path"] for entry in done.covered_files} == {"/data/logs/access.log.1"}


def test_dry_run_is_read_only(env, tmp_path):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\n")
    wire = _wire(guest, "/data/logs/access.log.1")
    store.close()
    checkpoint_dir = tmp_path / "checkpoint"
    readonly = CheckpointStore.for_history_dir(str(checkpoint_dir), read_only=True)
    try:
        with LokiServer() as server:
            result = hi.import_guest_logs(
                executor, _settings(server.url), readonly, KEY,
                name="npm-ct", files=[wire], dry_run=True, sleep=lambda _s: None,
            )
        assert server.requests == []
        assert executor.capture_calls == 0
        assert executor.snapshot_calls == 0
        assert result.pending is True
        assert result.failed is False
        assert result.covered_files == []
        assert any("archive coverage" in warning for warning in result.warnings)
    finally:
        readonly.close()


def test_long_escaped_and_non_utf8_roundtrip(env):
    guest, _spool, executor, store = env
    long_line = b"L" + b"\x00\xff\xfe binary " + b"z" * 40000 + b"\n"
    payload = b"short\n" + long_line
    _write(guest, "/data/logs/access.log.1", payload)
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
            sleep=lambda _s: None,
        )
    assert result.failed is False
    entries = list(_iter_entries(server))
    assert _reconstruct(entries) == payload
    # The binary slice is carried base64 rather than silently replaced.
    assert any("raw_base64" in entry for entry in entries)
    assert all(len(json.dumps(entry)) <= 128 * 1024 for entry in entries)


def test_concatenated_gzip_frames_each_marker(env):
    guest, _spool, executor, store = env
    frame_a = gzip.compress(b"marker-A\n")
    frame_b = gzip.compress(b"marker-B\n")
    _write(guest, "/data/logs/access.log.2.gz", frame_a + frame_b)
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.2.gz", compression="gzip")],
            sleep=lambda _s: None,
        )
    assert result.failed is False
    text = _reconstruct(list(_iter_entries(server)))
    assert text == b"marker-A\nmarker-B\n"


def test_zstd_archive_imports(env):
    guest, _spool, executor, store = env
    payload = b"zstd-one\nzstd-two\n"
    _write(guest, "/var/log/proxmox-backup/api/access.log.1.zst", zstandard.ZstdCompressor().compress(payload))
    _write(guest, "/var/log/proxmox-backup/tasks/.keep", b"")
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="pbs-ct",
            files=[_wire(guest, "/var/log/proxmox-backup/api/access.log.1.zst", compression="zstd", profile="pbs", log_kind="api")],
            sleep=lambda _s: None,
        )
    assert result.failed is False
    assert _reconstruct(list(_iter_entries(server))) == payload


def test_numeric_rename_reuses_coverage_without_replay(env):
    guest, _spool, executor, store = env
    source = _write(guest, "/data/logs/access.log.1", b"alpha\nbeta\n")
    with LokiServer() as server:
        hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
            sleep=lambda _s: None,
        )
    pushes_after_first = len(server.requests)
    # Native numeric rotation: access.log.1 -> access.log.2 (same inode).
    os.replace(source, guest / "data" / "logs" / "access.log.2")
    with LokiServer() as second:
        result = hi.import_guest_logs(
            executor, _settings(second.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.2")],
            sleep=lambda _s: None,
        )
    assert second.requests == []  # already acknowledged, not replayed
    assert pushes_after_first > 0
    assert {entry["path"] for entry in result.covered_files} == {"/data/logs/access.log.2"}


def test_replacement_and_truncation_are_fresh_generations(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"original\n")
    with LokiServer() as server:
        hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
            sleep=lambda _s: None,
        )
    # Replacement: a brand-new inode at the same path with different content.
    replacement = guest / "data" / "logs" / "replacement.tmp"
    replacement.write_bytes(b"replaced content\n")
    os.replace(replacement, guest / "data" / "logs" / "access.log.1")
    with LokiServer() as second:
        hi.import_guest_logs(
            executor, _settings(second.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
            sleep=lambda _s: None,
        )
    assert second.requests  # new generation imported
    assert b"replaced content" in b"".join(second.bodies)
    paths = [record.path for record in store.sources(KEY)]
    assert paths.count("/data/logs/access.log.1") == 2  # two generations


def test_compression_successor_proved_lineage(env):
    guest, _spool, executor, store = env
    payload = b"one\ntwo\nthree\n"
    _write(guest, "/data/logs/access.log.1", payload)
    with LokiServer() as server:
        hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
            sleep=lambda _s: None,
        )
    pushes_after_plain = len(server.requests)
    # Compression successor with identical decoded content: reuse, do not replay.
    _write(guest, "/data/logs/access.log.1.gz", gzip.compress(payload))
    with LokiServer() as second:
        result = hi.import_guest_logs(
            executor, _settings(second.url), store, KEY,
            name="npm-ct",
            files=[_wire(guest, "/data/logs/access.log.1.gz", compression="gzip")],
            sleep=lambda _s: None,
        )
    assert second.requests == []  # proved lineage -> no duplicate push
    assert pushes_after_plain > 0
    assert result.failed is False
    successor = next(entry for entry in result.covered_files if entry["path"] == "/data/logs/access.log.1.gz")
    assert re.fullmatch(r"[0-9a-f]{64}", successor["sha256"])  # raw compressed fingerprint

    # A *different* compression successor is imported normally (never guessed).
    _write(guest, "/data/logs/error.log.1", b"plain-error\n")
    _write(guest, "/data/logs/error.log.1.gz", gzip.compress(b"different content\n"))
    with LokiServer() as third:
        result2 = hi.import_guest_logs(
            executor, _settings(third.url), store, KEY,
            name="npm-ct",
            files=[
                _wire(guest, "/data/logs/error.log.1.gz", compression="gzip"),
            ],
            sleep=lambda _s: None,
        )
    assert third.requests  # not deduplicated against an unrelated file
    assert result2.failed is False


def test_active_prefix_partial_then_append_continuation(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log", b"hello wor", mode=0o644)
    wire_active = _wire(guest, "/data/logs/access.log", is_active=True)
    with LokiServer() as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[wire_active], sleep=lambda _s: None,
        )
    assert first.failed is False
    entries = list(_iter_entries(server))
    assert entries and entries[-1].get("partial") is True and entries[-1]["fragment_final"] is True
    # The immutable frozen prefix alone completes the initial-manifest gate...
    assert store.initial_manifest_state(KEY).acknowledged == 1
    # ...but the still-current live file is never prune-eligible.
    assert first.covered_files == []

    # The writer appends and the file is later closed: the partial line continues.
    with open(guest / "data" / "logs" / "access.log", "ab") as handle:
        handle.write(b"ld\n")
    wire_closed = _wire(guest, "/data/logs/access.log", is_active=False)
    with LokiServer() as second:
        result = hi.import_guest_logs(
            executor, _settings(second.url), store, KEY,
            name="npm-ct", files=[wire_closed], sleep=lambda _s: None,
        )
    assert result.failed is False
    all_entries = list(_iter_entries(server)) + list(_iter_entries(second))
    assert _reconstruct(all_entries) == b"hello world\n"
    # The appended current file is archived in full, but a current NPM `.log`
    # name stays uncovered even with no writer fd open.
    assert result.covered_files == []


def test_stale_pending_batch_is_retimestamped(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\nbeta\n")
    wire = _wire(guest, "/data/logs/access.log.1")
    clock = [1_700_000_000_000_000_000]
    with LokiServer(script=[(503, None), (503, None), (503, None)]) as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None, clock_ns=lambda: clock[0],
        )
    assert first.pending is True
    saved = store.pending_batch(KEY)
    assert saved is not None
    clock[0] += 25 * 60 * 60 * 1_000_000_000  # 25h later
    with LokiServer() as second:
        result = hi.import_guest_logs(
            executor, _settings(second.url), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None, clock_ns=lambda: clock[0],
        )
    assert result.failed is False and result.pending is False
    assert second.requests
    new_body = second.requests[0][1]
    assert new_body != saved.body  # timestamps reassigned
    assert b"alpha" in new_body and b"beta" in new_body
    assert store.initial_manifest_state(KEY).acknowledged == 1


def test_release_reclaims_acknowledged_blobs(env, tmp_path):
    guest, spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\n")
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
            sleep=lambda _s: None,
        )
    assert result.failed is False
    assert executor.release_calls == 1
    capture_id = store.capture_intent(KEY).capture_id
    capture_dir = Path(spool) / capture_id
    # Every acknowledged blob is reclaimed and the fully-released dir is gone.
    assert not capture_dir.exists()
    assert store.captured_blobs(KEY, capture_id)
    assert all(blob.released for blob in store.captured_blobs(KEY, capture_id))


def test_release_command_validation():
    valid = hi.capture_id_for(KEY)
    token = "a" * 64
    assert f"/{valid}/" in hi.build_capture_release_command(valid, [f"/var/tmp/fleet-log-import/{valid}/{token}.blob"])
    with pytest.raises(ValueError):
        hi.build_capture_release_command("bad-id", [f"/var/tmp/fleet-log-import/{valid}/{token}.blob"])
    with pytest.raises(ValueError):
        hi.build_capture_release_command(valid, [f"/var/tmp/fleet-log-import/{valid}/../escape.blob"])
    with pytest.raises(ValueError):
        hi.build_capture_release_command(valid, [])


def test_release_program_refuses_tampered_spool(tmp_path):
    guest = tmp_path / "guest"
    (guest / "data" / "logs").mkdir(parents=True)
    _write(guest, "/data/logs/access.log.1", b"alpha\n")
    spool = tmp_path / "spool"
    request = {
        "capture_id": hi.capture_id_for(KEY),
        "files": [],
    }
    wire = _wire(guest, "/data/logs/access.log.1")
    source_id = hi.base_source_id(wire["profile"], wire["log_kind"], wire["path"])
    request["files"] = [{**wire, "blob_path": hi.blob_path_for(request["capture_id"], source_id, spool_root=str(spool))}]
    facts = hio.capture(request, sysroot=str(guest), spool_root=str(spool))
    blob_path = facts["files"][0]["blob_path"]
    command = hi.build_capture_release_command(
        request["capture_id"], [blob_path], spool_root=str(spool)
    )
    # Tamper: a different file replaced the blob after capture.
    os.unlink(blob_path)
    with open(blob_path, "wb") as handle:
        handle.write(b"tampered")
    os.chmod(blob_path, 0o600)
    proc = subprocess.run(command, shell=True, capture_output=True, text=True)
    assert proc.returncode == 1
    assert "error" in json.loads(proc.stdout)
    assert os.path.exists(blob_path)


def test_empty_file_is_acknowledged(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/backend.log.1", b"")
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/backend.log.1")],
            sleep=lambda _s: None,
        )
    assert result.failed is False
    # No HTTP push is needed, but the manifest gate must still advance.
    assert server.requests == []
    assert store.initial_manifest_state(KEY).acknowledged == 1
    covered = {entry["path"] for entry in result.covered_files}
    assert covered == {"/data/logs/backend.log.1"}


def test_capture_resumes_after_failure(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\n")
    wire = _wire(guest, "/data/logs/access.log.1")
    executor.fail_capture = True
    with LokiServer() as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert first.failed is True
    first_id = store.capture_intent(KEY).capture_id

    executor.fail_capture = False
    with LokiServer() as second:
        result = hi.import_guest_logs(
            executor, _settings(second.url), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert result.failed is False
    # Same frozen capture id and manifest are reused, not replaced.
    assert store.capture_intent(KEY).capture_id == first_id
    assert {entry["path"] for entry in result.covered_files} == {"/data/logs/access.log.1"}


def test_task_index_is_imported_but_never_covered_for_prune(env):
    guest, _spool, executor, store = env
    _write(guest, "/var/log/proxmox-backup/tasks/archive", b"task summary\n")
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="pbs-ct",
            files=[_wire(guest, "/var/log/proxmox-backup/tasks/archive", profile="pbs", log_kind="task_index")],
            sleep=lambda _s: None,
        )
    assert result.failed is False
    assert server.requests  # task-index entries are archived
    assert result.covered_files == []  # but never prune-eligible


def test_retry_after_is_honoured_and_bounded(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\n")
    sleeps: List[float] = []
    with LokiServer(script=[(429, 3)]) as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
            sleep=sleeps.append,
        )
    assert result.failed is False
    assert sleeps == [3.0]
    assert len(server.requests) == 2  # 429 then 204


def test_blob_tamper_reports_conflict_as_failed(env):
    guest, spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\nbeta\n")
    wire = _wire(guest, "/data/logs/access.log.1")

    def tamper() -> None:
        capture_id = hi.capture_id_for(KEY)
        source_id = hi.base_source_id(wire["profile"], wire["log_kind"], wire["path"])
        blob = Path(hi.blob_path_for(capture_id, source_id, spool_root=str(spool)))
        with open(blob, "ab") as handle:
            handle.write(b"extra")

    executor.after_capture = tamper
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert result.failed is True
    assert result.covered_files == []
    assert any("conflict" in warning for warning in result.warnings)


def test_blob_prefix_survives_source_rotation(env):
    guest, _spool, executor, store = env
    source = _write(guest, "/data/logs/access.log.1", b"alpha\nbeta\n")
    wire = _wire(guest, "/data/logs/access.log.1")

    def rotate() -> None:
        os.unlink(source)

    executor.after_capture = rotate
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert result.failed is False
    assert _reconstruct(list(_iter_entries(server))) == b"alpha\nbeta\n"


def test_body_budget_caps_encoded_push_bytes(env):
    guest, _spool, executor, store = env
    tricky = b"".join((b"\x01\\\"" * 4000) + b"\n" for _ in range(200))
    _write(guest, "/data/logs/access.log.1", tricky)
    wire = _wire(guest, "/data/logs/access.log.1")
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url, budget_mb=1), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    body_total = sum(len(body) for body in server.bodies)
    assert body_total <= 1024 * 1024
    assert result.pending is True

def test_active_append_does_not_reopen_released_initial_blob(env):
    guest, _spool, executor, store = env
    log = _write(guest, "/data/logs/access.log", b"initial\n")
    with LokiServer() as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct",
            files=[_wire(guest, "/data/logs/access.log", is_active=True)],
        )
        assert not first.failed
        with log.open("ab") as handle:
            handle.write(b"still active\n")
        count = len(server.requests)
        second = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct",
            files=[_wire(guest, "/data/logs/access.log", is_active=True)],
        )
        assert not second.failed
        assert len(server.requests) == count
        assert store.initial_manifest_state(KEY).remaining == 0
        assert not second.covered_files


def test_replaced_partial_source_still_imports_frozen_initial_prefix_first(env):
    guest, _spool, executor, store = env
    original = (b"old " + b"x" * 15995 + b"\n") * 100
    log = _write(guest, "/data/logs/access.log.1", original)
    with LokiServer() as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url, budget_mb=1), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
        )
        assert first.pending
        log.unlink()
        log.write_bytes(b"replacement\n")
        second = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct",
            files=[_wire(guest, "/data/logs/access.log.1")],
        )
        assert not second.failed
        assert store.initial_manifest_state(KEY).remaining == 0
        entries = list(_iter_entries(server))
        replacement_index = next(i for i, entry in enumerate(entries) if entry["_entry"] == "replacement")
        assert _reconstruct(entries[:replacement_index]) == original


def test_append_with_rewritten_prefix_blocks_reuse(env):
    guest, _spool, executor, store = env
    log = _write(guest, "/data/logs/access.log", b"original\n")
    with LokiServer() as server:
        hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct",
            files=[_wire(guest, "/data/logs/access.log", is_active=True)],
        )
        count = len(server.requests)
        log.write_bytes(b"rewritten\nnew tail\n")
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct",
            files=[_wire(guest, "/data/logs/access.log")],
        )
        assert result.failed
        assert not result.covered_files
        assert len(server.requests) == count


def test_partial_initial_plain_compressed_before_resume_is_not_replayed(env):
    guest, _spool, executor, store = env
    payload = (b"original " + b"x" * 15990 + b"\n") * 100
    plain = _write(guest, "/data/logs/access.log.1", payload)
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url, budget_mb=1), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
        )
        assert result.pending
        compressed = _write(guest, "/data/logs/access.log.1.gz", gzip.compress(payload))
        plain.unlink()
        finished = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct",
            files=[_wire(guest, "/data/logs/access.log.1.gz", compression="gzip")],
        )
        assert not finished.failed
        assert store.initial_manifest_state(KEY).remaining == 0
        assert _reconstruct(list(_iter_entries(server))) == payload
        assert {row["path"] for row in finished.covered_files} == {"/data/logs/access.log.1.gz"}
        assert compressed.exists()


# --------------------------------------------------------------------------- #
# Current-identity coverage (the only prune authorisation)
# --------------------------------------------------------------------------- #


def test_current_npm_and_rotated_names_are_covered_selectively(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log", b"current-access\n")
    _write(guest, "/data/logs/backend.log", b"current-backend\n")
    _write(guest, "/data/logs/access.log.1", b"rotated-access\n")
    wires = [
        _wire(guest, "/data/logs/access.log", is_active=False),
        _wire(guest, "/data/logs/backend.log", is_active=False),
        _wire(guest, "/data/logs/access.log.1", is_active=False),
    ]
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=wires, sleep=lambda _s: None,
        )
    assert result.failed is False
    # Everything is archived...
    assert {entry["filename"] for entry in _iter_entries(server)} == {
        "/data/logs/access.log", "/data/logs/backend.log", "/data/logs/access.log.1",
    }
    # ...but a current `.log` name stays uncovered even with no writer fd open.
    assert {entry["path"] for entry in result.covered_files} == {"/data/logs/access.log.1"}
    covered = result.covered_files[0]
    assert re.fullmatch(r"[0-9a-f]{64}", covered["sha256"])
    assert covered["source_id"]


def test_current_pbs_api_logs_are_never_covered(env):
    guest, _spool, executor, store = env
    _write(guest, "/var/log/proxmox-backup/api/access.log", b"api access\n")
    _write(guest, "/var/log/proxmox-backup/api/auth.log", b"api auth\n")
    _write(guest, "/var/log/proxmox-backup/api/access.log.1", b"rotated api\n")
    wires = [
        _wire(guest, "/var/log/proxmox-backup/api/access.log", profile="pbs", log_kind="api"),
        _wire(guest, "/var/log/proxmox-backup/api/auth.log", profile="pbs", log_kind="api"),
        _wire(guest, "/var/log/proxmox-backup/api/access.log.1", profile="pbs", log_kind="api"),
    ]
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="pbs-ct", files=wires, sleep=lambda _s: None,
        )
    assert result.failed is False
    assert {entry["filename"] for entry in _iter_entries(server)} == {
        "/var/log/proxmox-backup/api/access.log",
        "/var/log/proxmox-backup/api/auth.log",
        "/var/log/proxmox-backup/api/access.log.1",
    }
    assert {entry["path"] for entry in result.covered_files} == {
        "/var/log/proxmox-backup/api/access.log.1"
    }


def test_covered_files_cross_check_against_durable_records(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log", b"live\n", mode=0o644)
    _write(guest, "/data/logs/access.log.1", b"one\ntwo\n")
    _write(guest, "/data/logs/error.log.2.gz", gzip.compress(b"three\nfour\n"))
    wires = [
        _wire(guest, "/data/logs/access.log", is_active=False),
        _wire(guest, "/data/logs/access.log.1"),
        _wire(guest, "/data/logs/error.log.2.gz", compression="gzip"),
    ]
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=wires, sleep=lambda _s: None,
        )
    assert result.failed is False
    records = {record.source_id: record for record in store.sources(KEY)}
    assert result.covered_files, "rotated archives must be covered"
    for entry in result.covered_files:
        record = records[entry["source_id"]]
        # The durable record must back every authorising entry exactly.
        assert record.acknowledged is True
        assert record.digest == entry["sha256"]
        assert (record.device, record.inode, record.size, record.mtime_ns) == (
            entry["device"], entry["inode"], entry["size"], entry["mtime_ns"],
        )
        assert record.log_kind != "task_index" and record.is_active is False
        assert sources.current_log_name(record.profile, record.log_kind, record.path) is False
    assert {entry["path"] for entry in result.covered_files} == {
        "/data/logs/access.log.1", "/data/logs/error.log.2.gz",
    }


def test_new_live_current_source_waits_for_close_or_rotation(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"old rotation\n")
    with LokiServer() as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log.1")],
            sleep=lambda _s: None,
        )
    assert first.failed is False
    pushes_after_first = len(server.requests)

    # A brand-new live current log and a new closed archive appear together.
    _write(guest, "/data/logs/error.log", b"live tail\n")
    _write(guest, "/data/logs/error.log.1", b"closed rotation\n")
    with LokiServer() as second:
        result = hi.import_guest_logs(
            executor, _settings(second.url), store, KEY,
            name="npm-ct",
            files=[
                _wire(guest, "/data/logs/error.log", is_active=True),
                _wire(guest, "/data/logs/error.log.1"),
            ],
            sleep=lambda _s: None,
        )
    assert result.failed is False
    # Only the closed file becomes an archive input; the live tail stays with
    # the live Alloy reader and is never recorded as an archive source.
    assert {entry["filename"] for entry in _iter_entries(second)} == {"/data/logs/error.log.1"}
    assert {entry["path"] for entry in result.covered_files} == {"/data/logs/error.log.1"}
    assert "/data/logs/error.log" not in {record.path for record in store.sources(KEY)}
    assert pushes_after_first > 0

    # Rotation later turns the live tail into a closed archive input.
    os.replace(guest / "data" / "logs" / "error.log", guest / "data" / "logs" / "error.log.2")
    with LokiServer() as third:
        rotated = hi.import_guest_logs(
            executor, _settings(third.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/error.log.2")],
            sleep=lambda _s: None,
        )
    assert rotated.failed is False
    assert {entry["filename"] for entry in _iter_entries(third)} == {"/data/logs/error.log.2"}
    assert {entry["path"] for entry in rotated.covered_files} == {"/data/logs/error.log.2"}


def test_replaced_current_tail_is_not_rearchived_live(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log", b"first generation\n")
    with LokiServer() as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log", is_active=True)],
            sleep=lambda _s: None,
        )
    assert first.failed is False
    pushes_after_first = len(server.requests)

    # The live current file is replaced in place by a brand-new inode.
    replacement = guest / "data" / "logs" / "replacement.tmp"
    replacement.write_bytes(b"second generation live\n")
    os.replace(replacement, guest / "data" / "logs" / "access.log")
    with LokiServer() as second:
        result = hi.import_guest_logs(
            executor, _settings(second.url), store, KEY,
            name="npm-ct", files=[_wire(guest, "/data/logs/access.log", is_active=True)],
            sleep=lambda _s: None,
        )
    assert second.requests == []
    assert result.covered_files == []
    assert pushes_after_first > 0
    # The replaced live tail creates no fresh-generation archive record.
    assert len([record for record in store.sources(KEY) if record.path == "/data/logs/access.log"]) == 1


# --------------------------------------------------------------------------- #
# Typed concepts: batch acknowledgement, frozen-prefix completion, coverage
# --------------------------------------------------------------------------- #




def test_current_log_name_classification():
    assert sources.current_log_name("npm", "application", "/data/logs/access.log") is True
    assert sources.current_log_name("npm", "application", "/data/logs/letsencrypt.log") is True
    assert sources.current_log_name("npm", "application", "/data/logs/access.log.1") is False
    assert sources.current_log_name("npm", "application", "/data/logs/access.log.1.gz") is False
    assert sources.current_log_name("npm", "application", "/data/logs/access.log-20240101T010101.gz") is False
    assert sources.current_log_name("pbs", "api", "/var/log/proxmox-backup/api/access.log") is True
    assert sources.current_log_name("pbs", "api", "/var/log/proxmox-backup/api/auth.log.3.zst") is False
    assert sources.current_log_name("pbs", "task", "/var/log/proxmox-backup/tasks/AB/UPID:x") is False
    assert sources.current_log_name("pbs", "task_index", "/var/log/proxmox-backup/tasks/archive") is False


def test_partial_http204_progress_never_completes_the_prefix(env):
    guest, _spool, executor, store = env
    payload = b"".join(f"row-{i:06d}-{'y' * 90}\n".encode() for i in range(12000))
    _write(guest, "/data/logs/access.log.1", payload)
    wire = _wire(guest, "/data/logs/access.log.1")
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url, budget_mb=1), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert result.pending is True and result.failed is False
    assert server.requests and all(path.endswith(hi.LOKI_PUSH_PATH) for path, _b, _h in server.requests)
    record = next(record for record in store.sources(KEY) if record.path == "/data/logs/access.log.1")
    provenance = sources.SourceProvenance.parse(record.provenance)
    assert 0 < record.decoded_offset < len(payload)
    # Bytes are durable in Loki, but the immutable prefix is not finished.
    assert record.acknowledged is False
    assert provenance.complete is False and provenance.complete_size == 0
    assert result.covered_files == []
    assert store.initial_manifest_state(KEY).remaining == 1

    with LokiServer() as second:
        done = hi.import_guest_logs(
            executor, _settings(second.url), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert done.failed is False and done.pending is False
    final = next(record for record in store.sources(KEY) if record.path == "/data/logs/access.log.1")
    final_provenance = sources.SourceProvenance.parse(final.provenance)
    assert final.acknowledged is True
    assert final_provenance.complete is True and final_provenance.complete_size == len(payload)
    assert {entry["path"] for entry in done.covered_files} == {"/data/logs/access.log.1"}


def test_pending_body_and_tombstone_survive_crash_until_acknowledged(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/access.log.1", b"alpha\nbeta\n")
    wire = _wire(guest, "/data/logs/access.log.1")
    db_path = store.path
    with LokiServer(script=[(503, None), (503, None), (503, None)]) as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[wire], sleep=lambda _s: None,
        )
    assert first.pending is True and first.failed is True
    pending = store.pending_batch(KEY)
    assert pending is not None
    saved_body = pending.body
    source_id = pending.progress[0].source_id

    # Simulated crash: reopen the same database, pending bytes must survive.
    store.close()
    store = CheckpointStore.open(db_path)
    try:
        reopened = store.pending_batch(KEY)
        assert reopened is not None and reopened.body == saved_body
        # The unacknowledged source owns an unresolved retry: a tombstone that
        # still references it is retained regardless of the 30-day window.
        store.mark_source_absent(KEY, source_id, ts_ns=1)
        assert store.purge_tombstones(now_ts_ns=100 * 24 * 60 * 60 * 1_000_000_000) == 0
        assert store.source(KEY, source_id) is not None

        with LokiServer() as second:
            resumed = hi.import_guest_logs(
                executor, _settings(second.url), store, KEY,
                name="npm-ct", files=[wire], sleep=lambda _s: None,
            )
        assert resumed.failed is False and resumed.pending is False
        assert second.requests and second.requests[0][1] == saved_body
        assert store.pending_batch(KEY) is None
        assert store.initial_manifest_state(KEY).acknowledged == 1
    finally:
        store.close()
