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
import hashlib
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
from proxmox_fleet.housekeeping_checkpoint import (
    CheckpointStore,
    GuestKey,
    PruneIntent,
    SourceIdentity,
)
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


def _stream_records(server: LokiServer):
    """Yield ``(stream_labels, entry)`` for every value in every pushed body."""
    for _path, body, _headers in server.requests:
        data = json.loads(body.decode("utf-8"))
        for stream in data["streams"]:
            for _ts, line in stream["values"]:
                yield stream["stream"], json.loads(line)


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


def test_recreated_live_path_preserves_rotated_prefix_and_repeat_is_idle(env):
    guest, _spool, executor, store = env
    current = "/data/logs/access.log"
    rotated = "/data/logs/access.log-20261009T211137"
    head, tail = b"acknowledged head\n", b"new closed tail\n"
    original = _write(guest, current, head)
    with LokiServer() as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct",
            files=[_wire(guest, current, is_active=True)],
        )
        assert not first.failed
        source_id = store.sources(KEY)[0].source_id
        with original.open("ab") as handle:
            handle.write(tail)
        original.rename(guest / rotated.lstrip("/"))
        _write(guest, current, b"fresh live stream\n")
        wires = [
            _wire(guest, current, is_active=True),
            _wire(guest, rotated),
        ]
        server.requests.clear()
        closed = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct", files=wires,
        )
        assert not closed.failed
        assert _reconstruct(_iter_entries(server)) == tail
        assert [entry["filename"] for entry in _iter_entries(server)] == [current]
        assert closed.covered_files[0]["source_id"] == source_id
        assert closed.covered_files[0]["sha256"] == hashlib.sha256(head + tail).hexdigest()
        server.requests.clear()
        repeated = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct", files=wires,
        )
        assert not repeated.failed
        assert server.requests == []
        assert repeated.bytes_archived == 0
        assert repeated.covered_files == closed.covered_files


def test_compression_uses_latest_generation_not_newer_absence_timestamp(env):
    guest, _spool, executor, store = env
    path = "/data/logs/access.log.1"
    old = _write(guest, path, b"old generation\n")
    with LokiServer() as server:
        hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct",
            files=[_wire(guest, path)],
        )
        old_id = store.sources(KEY)[0].source_id
        old.rename(guest / "held-old-inode")
        payload = b"replacement generation, fully acknowledged\n"
        current = _write(guest, path, payload)
        replacement = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct",
            files=[_wire(guest, path)],
        )
        assert not replacement.failed
        latest = store.source(KEY, replacement.covered_files[0]["source_id"])
        # A later observation (or skewed clock) can refresh an old tombstone;
        # it must not make that older content the compression predecessor.
        store.clear_source_absent(KEY, old_id)
        store.mark_source_absent(KEY, old_id, ts_ns=latest.updated_ns + 3_600_000_000_000)
        current.rename(guest / "held-current-inode")
        compressed = path + ".gz"
        _write(guest, compressed, gzip.compress(payload))
        server.requests.clear()
        successor = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="npm-ct",
            files=[_wire(guest, compressed, compression="gzip")],
        )
        assert not successor.failed
        assert server.requests == []
        assert successor.covered_files[0]["sha256"] == hashlib.sha256(
            (guest / compressed.lstrip("/")).read_bytes()
        ).hexdigest()


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
    capture_id = store.capture_intent(KEY).capture_id
    capture_dir = Path(spool) / capture_id
    # Every acknowledged blob is reclaimed and the fully-released dir is gone.
    assert not capture_dir.exists()
    assert store.captured_blobs(KEY, capture_id)
    assert all(blob.released for blob in store.captured_blobs(KEY, capture_id))


def test_large_initial_manifest_releases_all_acknowledged_blobs(env):
    guest, spool, executor, store = env
    payloads = {
        f"/data/logs/access-{index:04d}.log.1": f"retained-{index}\n".encode()
        for index in range(1000)
    }
    for path, payload in payloads.items():
        _write(guest, path, payload)
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, path) for path in payloads],
            sleep=lambda _s: None,
        )
    assert not result.failed and not result.pending
    archived = {entry["filename"]: entry["_entry"] + "\n" for entry in _iter_entries(server)}
    assert archived == {path: payload.decode() for path, payload in payloads.items()}
    capture_id = store.capture_intent(KEY).capture_id
    assert all(blob.released for blob in store.captured_blobs(KEY, capture_id))
    assert not (spool / capture_id).exists()
    assert {path: (guest / path.lstrip("/")).read_bytes() for path in payloads} == payloads


def test_frozen_prefix_batch_completes_when_transport_goes_offline_after_fetch(env):
    guest, spool, _executor, store = env

    class OfflineAfterFetchExecutor(FakeExecutor):
        offline = False

        def housekeeping_snapshot(self, lxc_id: str, *, files, destination: str) -> PrimitiveResult:
            if self.offline:
                return _failed("node transport offline")
            result = super().housekeeping_snapshot(lxc_id, files=files, destination=destination)
            self.offline = True
            return result

    payloads = {
        f"/data/logs/batch-{index}.log.1": f"distinct-frozen-content-{index}\n".encode()
        for index in range(4)
    }
    for path, payload in payloads.items():
        _write(guest, path, payload)
    executor = OfflineAfterFetchExecutor(guest, spool)
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, path) for path in payloads],
            sleep=lambda _s: None,
        )
    assert not result.failed and not result.pending
    assert {
        entry["filename"]: entry["_entry"] + "\n" for entry in _iter_entries(server)
    } == {path: payload.decode() for path, payload in payloads.items()}
    capture_id = store.capture_intent(KEY).capture_id
    assert all(blob.released for blob in store.captured_blobs(KEY, capture_id))
    assert not (spool / capture_id).exists()
    assert {path: (guest / path.lstrip("/")).read_bytes() for path in payloads} == payloads


def test_pooled_batch_binds_distinct_metadata_and_bodies(env):
    """One pooled fetch feeds four sources; every push keeps its own identity."""
    guest, spool, executor, store = env
    payloads = {
        f"/data/logs/bind-{index}.log.1": f"pooled-distinct-{index}\n".encode()
        for index in range(4)
    }
    for path, payload in payloads.items():
        _write(guest, path, payload)
    wires = [_wire(guest, path) for path in payloads]
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=wires, sleep=lambda _s: None,
        )
    assert not result.failed and not result.pending

    by_filename: Dict[str, Tuple[Dict[str, str], Dict[str, Any]]] = {}
    for labels, entry in _stream_records(server):
        assert entry["filename"] not in by_filename
        by_filename[entry["filename"]] = (labels, entry)
    assert set(by_filename) == set(payloads)
    for path, payload in payloads.items():
        labels, entry = by_filename[path]
        # Identity labels are per-source, never shared/swapped between members.
        assert labels == {
            "job": "lxc-file",
            "delivery": "archive",
            "cluster": KEY.cluster,
            "node": KEY.node,
            "guest_id": KEY.lxc_id,
            "host": "npm-ct",
            "app": "nginxproxymanager",
            "log_kind": "application",
        }
        assert entry["_entry"] == payload.decode("utf-8").rstrip("\n")
        # Each source carries its own durable digest; the frozen prefix is
        # acknowledged from its own body, not from the pooled prefetch.
        source_id = hi.base_source_id("npm", "application", path)
        record = store.source(KEY, source_id)
        assert record is not None
        assert record.digest == hashlib.sha256(payload).hexdigest()
        assert sources.is_prefix_complete(record)
        assert entry["source_id"] == source_id
    digests = {
        store.source(KEY, hi.base_source_id("npm", "application", path)).digest
        for path in payloads
    }
    assert len(digests) == len(payloads)

    capture_id = store.capture_intent(KEY).capture_id
    assert all(blob.released for blob in store.captured_blobs(KEY, capture_id))
    assert not (spool / capture_id).exists()
    assert {path: (guest / path.lstrip("/")).read_bytes() for path in payloads} == payloads


def test_pooled_prefetch_never_credits_without_http_ack(env):
    """Prefetched frozen bytes are worthless until the source's own push lands."""
    guest, spool, _executor, store = env

    class OfflineAfterFetchExecutor(FakeExecutor):
        offline = False

        def housekeeping_snapshot(self, lxc_id: str, *, files, destination: str) -> PrimitiveResult:
            if self.offline:
                return _failed("node transport offline")
            result = super().housekeeping_snapshot(lxc_id, files=files, destination=destination)
            self.offline = True
            return result

    payloads = {
        f"/data/logs/unacked-{index}.log.1": f"prefetched-but-unacked-{index}\n".encode()
        for index in range(4)
    }
    for path, payload in payloads.items():
        _write(guest, path, payload)
    executor = OfflineAfterFetchExecutor(guest, spool)
    with LokiServer(default=503) as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, path) for path in payloads],
            sleep=lambda _s: None,
        )
    # The node batch was fetched (transport is now offline for good), yet a
    # missing 204 must leave every source unacknowledged and unreleasable.
    assert result.failed is True
    assert result.covered_files == []
    assert store.initial_manifest_state(KEY).acknowledged == 0
    capture_id = store.capture_intent(KEY).capture_id
    blobs = store.captured_blobs(KEY, capture_id)
    assert blobs and all(not blob.released for blob in blobs)
    assert all(Path(blob.blob_path).exists() for blob in blobs)
    assert (spool / capture_id).exists()
    for path in payloads:
        record = store.source(KEY, hi.base_source_id("npm", "application", path))
        assert record is None or not record.acknowledged
    assert {path: (guest / path.lstrip("/")).read_bytes() for path in payloads} == payloads


def test_pooled_budget_resumes_and_releases_only_acknowledged(env):
    guest, spool, executor, store = env
    small_path = "/data/logs/a-small.log.1"
    large_path = "/data/logs/z-large.log.1"
    _write(guest, small_path, b"small-acknowledged\n")
    large = b"".join(f"budget-row-{index:06d}-{'x' * 80}\n".encode() for index in range(20000))
    _write(guest, large_path, large)
    wires = [_wire(guest, small_path), _wire(guest, large_path)]

    with LokiServer() as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url, budget_mb=1), store, KEY,
            name="npm-ct", files=wires, sleep=lambda _s: None,
        )
    assert first.pending and not first.failed
    capture_id = store.capture_intent(KEY).capture_id
    blobs = {blob.source_path: blob for blob in store.captured_blobs(KEY, capture_id)}
    assert blobs[small_path].released and not Path(blobs[small_path].blob_path).exists()
    assert not blobs[large_path].released and Path(blobs[large_path].blob_path).read_bytes() == large

    with LokiServer() as second:
        done = hi.import_guest_logs(
            executor, _settings(second.url, budget_mb=1024), store, KEY,
            name="npm-ct", files=wires, sleep=lambda _s: None,
        )
    assert not done.failed and not done.pending
    # The already-acknowledged small file is never replayed, and the resumed
    # large file reconstructs exactly once with no gap or duplication.
    small_entries = [
        entry
        for srv in (server, second)
        for _labels, entry in _stream_records(srv)
        if entry["filename"] == small_path
    ]
    assert len(small_entries) == 1
    archived_large = _reconstruct(
        entry
        for srv in (server, second)
        for _labels, entry in _stream_records(srv)
        if entry["filename"] == large_path
    )
    assert archived_large == large
    assert all(blob.released for blob in store.captured_blobs(KEY, capture_id))
    assert not (spool / capture_id).exists()
    assert (guest / large_path.lstrip("/")).read_bytes() == large


def test_pooled_transport_failure_preserves_unacknowledged_history(env):
    guest, spool, _executor, store = env

    class AlwaysOfflineExecutor(FakeExecutor):
        def housekeeping_snapshot(self, lxc_id: str, *, files, destination: str) -> PrimitiveResult:
            return _failed("node transport offline")

    payloads = {
        f"/data/logs/offline-{index}.log.1": f"preserved-{index}\n".encode()
        for index in range(3)
    }
    for path, payload in payloads.items():
        _write(guest, path, payload)
    executor = AlwaysOfflineExecutor(guest, spool)
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=[_wire(guest, path) for path in payloads],
            sleep=lambda _s: None,
        )
    assert result.failed is True
    assert result.covered_files == []
    assert server.requests == []
    capture_id = store.capture_intent(KEY).capture_id
    blobs = store.captured_blobs(KEY, capture_id)
    assert len(blobs) == len(payloads)
    assert all(not blob.released for blob in blobs)
    assert all(Path(blob.blob_path).exists() for blob in blobs)
    assert (spool / capture_id).exists()
    assert {path: (guest / path.lstrip("/")).read_bytes() for path in payloads} == payloads


def test_acknowledged_blob_release_survives_transport_interruption(env):
    guest, spool, executor, store = env

    class InterruptedExecutor(FakeExecutor):
        interrupted = False

        def run_shell(self, command: str, **opts: Any) -> PrimitiveResult:
            if self.interrupted:
                return _failed("transport interrupted")
            result = super().run_shell(command, **opts)
            self.interrupted = True
            return result

    payloads = {
        f"/data/logs/retained-{index:04d}.log.1": f"resume-{index}\n".encode()
        for index in range(256)
    }
    for path, payload in payloads.items():
        _write(guest, path, payload)
    wires = [_wire(guest, path) for path in payloads]
    interrupted = InterruptedExecutor(guest, spool)
    with LokiServer() as server:
        first = hi.import_guest_logs(
            interrupted, _settings(server.url), store, KEY,
            name="npm-ct", files=wires, sleep=lambda _s: None,
        )
        assert not first.failed and not first.pending
        capture_id = store.capture_intent(KEY).capture_id
        blobs = store.captured_blobs(KEY, capture_id)
        assert 0 < sum(blob.released for blob in blobs) < len(payloads)
        assert all(Path(blob.blob_path).exists() != blob.released for blob in blobs)
        assert (spool / capture_id).exists()
        done = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY,
            name="npm-ct", files=wires, sleep=lambda _s: None,
        )
    assert not done.failed and not done.pending
    assert done.bytes_archived == 0
    assert all(blob.released for blob in store.captured_blobs(KEY, capture_id))
    assert not (spool / capture_id).exists()
    entries = list(_iter_entries(server))
    assert len(entries) == len(payloads)
    assert {entry["filename"]: entry["_entry"] + "\n" for entry in entries} == {
        path: payload.decode() for path, payload in payloads.items()
    }
    assert {path: (guest / path.lstrip("/")).read_bytes() for path in payloads} == payloads


def test_completed_frozen_blob_released_while_other_prefixes_need_backfill(env):
    guest, _spool, executor, store = env
    _write(guest, "/data/logs/a-small.log.1", b"fully-acknowledged\n")
    large = b"".join(f"pending-{index:06d}-{'x' * 80}\n".encode() for index in range(20000))
    _write(guest, "/data/logs/z-large.log.1", large)
    wires = [
        _wire(guest, "/data/logs/a-small.log.1"),
        _wire(guest, "/data/logs/z-large.log.1"),
    ]
    with LokiServer() as server:
        result = hi.import_guest_logs(
            executor, _settings(server.url, budget_mb=1), store, KEY,
            name="npm-ct", files=wires, sleep=lambda _s: None,
        )
    assert result.pending and not result.failed
    assert store.pending_batch(KEY) is None
    capture_id = store.capture_intent(KEY).capture_id
    blobs = {blob.source_path: blob for blob in store.captured_blobs(KEY, capture_id)}
    small = blobs["/data/logs/a-small.log.1"]
    unfinished = blobs["/data/logs/z-large.log.1"]
    assert small.released and not Path(small.blob_path).exists()
    assert not unfinished.released and Path(unfinished.blob_path).read_bytes() == large
    assert (guest / "data/logs/a-small.log.1").read_bytes() == b"fully-acknowledged\n"
    assert (guest / "data/logs/z-large.log.1").read_bytes() == large


def test_release_command_validation():
    valid = hi.capture_id_for(KEY)
    token = "a" * 64
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


# --------------------------------------------------------------------------- #
# PBS task logical identity vs. device+inode re-use
# --------------------------------------------------------------------------- #

PBS_TASK_ROOT = "/var/log/proxmox-backup/tasks"
OLD_UPID = "UPID:pbs:000000E6:00001AD4:00000008:69EF5DD5:backup:ct-100:root@pam:"
NEW_UPID = "UPID:pbs:000000F5:00003BD7:000004D2:6AC8D749:backup:ct-101:root@pam:"


def _task_path(fanout: str, upid: str) -> str:
    return f"{PBS_TASK_ROOT}/{fanout}/{upid}"


def _reuse_inode(guest: Path, old_path: str, new_path: str, new_data: bytes) -> None:
    """Recreate *new_path* on the exact inode *old_path* occupied."""
    old = guest / old_path.lstrip("/")
    new = guest / new_path.lstrip("/")
    new.parent.mkdir(parents=True, exist_ok=True)
    before = os.stat(old)
    os.link(old, new)
    os.unlink(old)
    new.write_bytes(new_data)
    after = os.stat(new)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def _archive_one(executor, store, url: str, guest: Path, path: str, key: GuestKey = KEY):
    return hi.import_guest_logs(
        executor, _settings(url), store, key, name="pbs-ct",
        files=[_wire(guest, path, profile="pbs", log_kind="task")],
        sleep=lambda _s: None,
    )


def _poison_reassigned(
    guest: Path,
    store: CheckpointStore,
    *,
    old_path: str,
    old_id: str,
    old_record,
    new_path: str,
    proof: str,
) -> None:
    """Reproduce the bug's rewrite of a canonical PBS task row onto a new task."""
    st = os.stat(guest / new_path.lstrip("/"))
    provenance = sources.SourceProvenance.parse(old_record.provenance)
    buggy = SourceIdentity(
        path=new_path,
        device=int(st.st_dev),
        inode=int(st.st_ino),
        size=int(st.st_size),
        mtime_ns=int(st.st_mtime_ns),
        allocated_bytes=int(st.st_blocks) * 512,
        compression="plain",
        profile="pbs",
        log_kind="task",
        is_active=False,
    )
    alias = {
        "path": new_path,
        "device": int(st.st_dev),
        "inode": int(st.st_ino),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }
    store.record_source(
        KEY,
        old_id,
        buggy,
        provenance=provenance.with_verification(
            verified_raw_size=0, conflict=True
        ).as_dict(),
        aliases=[alias],
        in_initial_manifest=old_record.in_initial_manifest,
        capture_id=old_record.capture_id,
        blob_path=old_record.blob_path,
    )
    digest = "0" * 64 if proof == "mismatch" else old_record.digest
    intent = PruneIntent(
        intent_id="poison-1",
        key=KEY,
        path=old_path,
        quarantine_path="/var/tmp/fleet-housekeeping-quarantine/poison-1",
        device=old_record.device,
        inode=old_record.inode,
        size=provenance.complete_size,
        mtime_ns=old_record.mtime_ns,
        source_id=old_id,
        digest=digest,
        state="pending",
    )
    store.record_prune_intent(intent)
    if proof in ("done", "absent"):
        store.resolve_prune_intent("poison-1", proof)


@pytest.mark.parametrize("new_data", [b"unrelated-new-task\nsecond-line\n", b"old-task-alpha\nold-task-beta\n"])
def test_distinct_pbs_task_reusing_inode_gets_independent_identity(env, new_data: bytes) -> None:
    """A new task that re-used a reclaimed task's inode never borrows its ACK."""
    guest, _spool, executor, store = env
    old_path = _task_path("D4", OLD_UPID)
    old_data = b"old-task-alpha\nold-task-beta\n"
    new_path = _task_path("D7", NEW_UPID)
    _write(guest, old_path, old_data)

    with LokiServer() as server:
        first = _archive_one(executor, store, server.url, guest, old_path)
    assert first.failed is False
    old_id = hi.base_source_id("pbs", "task", old_path)
    old_record = store.source(KEY, old_id)
    assert old_record is not None and old_record.acknowledged
    assert old_record.digest == hashlib.sha256(old_data).hexdigest()
    assert {row["path"] for row in first.covered_files} == {old_path}

    # The old task is reclaimed, then a different task re-uses its inode.
    _reuse_inode(guest, old_path, new_path, new_data)

    with LokiServer() as second:
        result = _archive_one(executor, store, second.url, guest, new_path)
    assert result.failed is False

    new_id = hi.base_source_id("pbs", "task", new_path)
    assert new_id != old_id
    new_record = store.source(KEY, new_id)
    assert new_record is not None and new_record.acknowledged
    assert new_record.digest == hashlib.sha256(new_data).hexdigest()

    # The original canonical frozen/ACK record is retained, untouched.
    retained = store.source(KEY, old_id)
    assert retained.path == old_path
    assert retained.digest == old_record.digest
    assert sources.SourceProvenance.parse(retained.provenance).complete is True

    # Only the new task is covered, on its own full-content fingerprint.
    assert {row["path"] for row in result.covered_files} == {new_path}
    assert result.covered_files[0]["sha256"] == hashlib.sha256(new_data).hexdigest()
    # Its bytes are archived whole, from offset zero; the old prefix is not replayed.
    assert _reconstruct(_iter_entries(second)) == new_data


@pytest.mark.parametrize(("old_kind", "new_kind"), [("api", "task"), ("task", "api")])
def test_reused_inode_cannot_cross_log_namespaces(env, old_kind: str, new_kind: str) -> None:
    guest, _spool, executor, store = env
    paths = {
        "api": "/var/log/proxmox-backup/api/access.log.2",
        "task": _task_path("D4", OLD_UPID),
    }
    old_path, new_path = paths[old_kind], paths[new_kind]
    old_data, new_data = b"old namespace\n", b"independent new namespace\n"
    _write(guest, old_path, old_data)
    with LokiServer() as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="pbs",
            files=[_wire(guest, old_path, profile="pbs", log_kind=old_kind)],
            sleep=lambda _s: None,
        )
    assert not first.failed
    old_id = hi.base_source_id("pbs", old_kind, old_path)
    old_record = store.source(KEY, old_id)
    assert old_record is not None and old_record.acknowledged
    _reuse_inode(guest, old_path, new_path, new_data)
    with LokiServer() as server:
        second = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="pbs",
            files=[_wire(guest, new_path, profile="pbs", log_kind=new_kind)],
            sleep=lambda _s: None,
        )
    assert not second.failed
    new_id = hi.base_source_id("pbs", new_kind, new_path)
    new_record = store.source(KEY, new_id)
    assert new_record is not None and new_record.acknowledged
    assert new_record.digest == hashlib.sha256(new_data).hexdigest()
    assert _reconstruct(_iter_entries(server)) == new_data
    retained = store.source(KEY, old_id)
    assert retained.path == old_path
    assert retained.digest == old_record.digest
    assert {row["path"] for row in second.covered_files} == {new_path}


def test_pbs_same_task_rename_and_append_stays_one_source(env):
    """A legitimate same-UPID rename/append keeps lineage and is not replayed."""
    guest, _spool, executor, store = env
    path_a = _task_path("D4", OLD_UPID)
    initial = b"task-line-one\n"
    _write(guest, path_a, initial)
    with LokiServer() as server:
        first = _archive_one(executor, store, server.url, guest, path_a)
    assert first.failed is False
    source_id = hi.base_source_id("pbs", "task", path_a)

    path_b = _task_path("E4", OLD_UPID)
    (guest / path_b.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
    os.rename(guest / path_a.lstrip("/"), guest / path_b.lstrip("/"))
    with open(guest / path_b.lstrip("/"), "ab") as handle:
        handle.write(b"task-line-two\n")

    with LokiServer() as second:
        result = _archive_one(executor, store, second.url, guest, path_b)
    assert result.failed is False
    records = [r for r in store.sources(KEY) if r.profile == "pbs" and r.log_kind == "task"]
    assert len(records) == 1
    assert records[0].source_id == source_id
    # Only the appended tail is pushed; the acknowledged prefix is never replayed.
    assert _reconstruct(_iter_entries(second)) == b"task-line-two\n"
    assert {row["path"] for row in result.covered_files} == {path_b}
    assert result.covered_files[0]["sha256"] == hashlib.sha256(initial + b"task-line-two\n").hexdigest()


def test_pbs_same_task_prefix_mutation_blocks(env):
    """A rewritten prefix on the same task path stays fail-closed."""
    guest, _spool, executor, store = env
    path = _task_path("D4", OLD_UPID)
    _write(guest, path, b"alpha\nbeta\n")
    with LokiServer() as server:
        first = _archive_one(executor, store, server.url, guest, path)
    assert first.failed is False
    count = len(server.requests)

    _write(guest, path, b"REWRITTEN-prefix\nnew-tail\n")
    with LokiServer() as second:
        result = _archive_one(executor, store, second.url, guest, path)
    assert result.failed is True
    assert result.covered_files == []
    assert second.requests == [] and len(server.requests) == count


def test_resumed_poisoned_task_row_is_restored_from_durable_proof(env):
    """A row already rewritten to another task recovers only from exact proof."""
    guest, _spool, executor, store = env
    old_path = _task_path("D4", OLD_UPID)
    old_data = b"old-task\n"
    new_path = _task_path("D7", NEW_UPID)
    new_data = b"different-task-bytes\nline-two\n"
    _write(guest, old_path, old_data)
    with LokiServer() as server:
        first = _archive_one(executor, store, server.url, guest, old_path)
    assert first.failed is False

    old_id = hi.base_source_id("pbs", "task", old_path)
    old_record = store.source(KEY, old_id)
    assert old_record is not None and old_record.digest is not None
    _reuse_inode(guest, old_path, new_path, new_data)
    _poison_reassigned(
        guest, store, old_path=old_path, old_id=old_id, old_record=old_record,
        new_path=new_path, proof="done",
    )
    poisoned = store.source(KEY, old_id)
    assert poisoned is not None
    assert poisoned.path == new_path and poisoned.digest == old_record.digest
    assert sources.SourceProvenance.parse(poisoned.provenance).coverage_conflict is True

    with LokiServer() as second:
        result = _archive_one(executor, store, second.url, guest, new_path)
    assert result.failed is False

    restored = store.source(KEY, old_id)
    assert restored is not None
    assert restored.path == old_path
    assert restored.digest == old_record.digest
    provenance = sources.SourceProvenance.parse(restored.provenance)
    assert provenance.complete is True and provenance.coverage_conflict is False
    assert restored.absent is True

    new_id = hi.base_source_id("pbs", "task", new_path)
    assert new_id != old_id
    new_record = store.source(KEY, new_id)
    assert new_record is not None and new_record.acknowledged
    assert new_record.digest == hashlib.sha256(new_data).hexdigest()
    assert {row["path"] for row in result.covered_files} == {new_path}
    assert _reconstruct(_iter_entries(second)) == new_data


@pytest.mark.parametrize("proof", ["pending", "absent"])
def test_task_archive_capsule_repairs_without_inferring_owned_deletion(env, proof: str) -> None:
    """An archived generation can be proved without claiming who deleted it."""
    guest, _spool, executor, store = env
    old_path = _task_path("D4", OLD_UPID)
    new_path = _task_path("D7", NEW_UPID)
    new_data = b"pending-proof-task\n"
    _write(guest, old_path, b"old-task\n")
    with LokiServer() as server:
        first = _archive_one(executor, store, server.url, guest, old_path)
    assert first.failed is False

    old_id = hi.base_source_id("pbs", "task", old_path)
    old_record = store.source(KEY, old_id)
    assert old_record is not None and old_record.digest is not None
    _reuse_inode(guest, old_path, new_path, new_data)
    _poison_reassigned(
        guest, store, old_path=old_path, old_id=old_id, old_record=old_record,
        new_path=new_path, proof=proof,
    )

    with LokiServer() as second:
        result = _archive_one(executor, store, second.url, guest, new_path)
    assert result.failed is False  # reachable, not blocked

    restored = store.source(KEY, old_id)
    assert restored is not None
    assert restored.path == old_path
    assert restored.digest == old_record.digest
    assert sources.SourceProvenance.parse(restored.provenance).complete is True
    assert restored.absent is False
    assert store.prune_intent("poison-1").state == proof

    new_id = hi.base_source_id("pbs", "task", new_path)
    assert new_id != old_id
    new_record = store.source(KEY, new_id)
    assert new_record is not None and new_record.acknowledged
    assert new_record.digest == hashlib.sha256(new_data).hexdigest()
    assert _reconstruct(_iter_entries(second)) == new_data


def test_poisoned_task_row_with_mismatching_proof_stays_blocked(env):
    """A mismatching capsule is not proof: the row is never guessed at."""
    guest, _spool, executor, store = env
    old_path = _task_path("D4", OLD_UPID)
    new_path = _task_path("D7", NEW_UPID)
    new_data = b"cannot-trust-me\n"
    _write(guest, old_path, b"old-task\n")
    with LokiServer() as server:
        first = _archive_one(executor, store, server.url, guest, old_path)
    assert first.failed is False

    old_id = hi.base_source_id("pbs", "task", old_path)
    old_record = store.source(KEY, old_id)
    assert old_record is not None and old_record.digest is not None
    _reuse_inode(guest, old_path, new_path, new_data)
    _poison_reassigned(
        guest, store, old_path=old_path, old_id=old_id, old_record=old_record,
        new_path=new_path, proof="mismatch",
    )

    with LokiServer() as second:
        result = _archive_one(executor, store, second.url, guest, new_path)
    assert result.failed is True
    # The known row is not mutated on a guess; the new task is still archived
    # under its own identity, from zero, on its own fingerprint.
    untrusted = store.source(KEY, old_id)
    assert untrusted is not None and untrusted.path == new_path
    assert store.prune_intent("poison-1").state == "pending"
    new_id = hi.base_source_id("pbs", "task", new_path)
    new_record = store.source(KEY, new_id)
    assert new_record is not None and new_record.acknowledged
    assert new_record.digest == hashlib.sha256(new_data).hexdigest()
    assert _reconstruct(_iter_entries(second)) == new_data
    # An unrepairable row must not shadow the independently acknowledged task.
    with LokiServer() as repeated:
        resumed = _archive_one(executor, store, repeated.url, guest, new_path)
    assert resumed.failed
    assert repeated.requests == []
    assert store.source(KEY, new_id).acknowledged
    assert store.source(KEY, new_id).digest == hashlib.sha256(new_data).hexdigest()
    audited = hi.import_guest_logs(
        executor, _settings(repeated.url), store, KEY, name="pbs",
        files=[_wire(guest, new_path, profile="pbs", log_kind="task")],
        dry_run=True, sleep=lambda _s: None,
    )
    assert {row["path"] for row in audited.covered_files} == {new_path}


def test_cross_cluster_task_identity_is_isolated(env):
    """Identical node/LXC ids in another cluster keep independent task rows."""
    guest, _spool, executor, store = env
    path = _task_path("D4", OLD_UPID)
    _write(guest, path, b"cluster-a task\n")
    other = GuestKey("cluster-b", "node-1", "120")
    with LokiServer() as server:
        _archive_one(executor, store, server.url, guest, path)
    with LokiServer() as second:
        _archive_one(executor, store, second.url, guest, path, key=other)
    source_id = hi.base_source_id("pbs", "task", path)
    assert store.source(KEY, source_id) is not None
    assert store.source(other, source_id) is not None
    assert store.source(KEY, source_id).key == KEY
    assert store.source(other, source_id).key == other


@pytest.mark.parametrize(
    ("suffix", "compression"),
    [(".gz", "gzip"), (".zst", "zstd"), (".zstd", "zstd")],
)
def test_pbs_task_compression_preserves_logical_identity_without_replay(
    env, suffix: str, compression: str,
) -> None:
    guest, _spool, executor, store = env
    path = _task_path("D4", OLD_UPID)
    payload = b"task started\ntask completed\n"
    plain = _write(guest, path, payload)
    with LokiServer() as server:
        first = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="pbs",
            files=[_wire(guest, path, profile="pbs", log_kind="task")],
            sleep=lambda _s: None,
        )
    assert not first.failed
    assert first.bytes_archived == len(payload)
    old = next(record for record in store.sources(KEY) if record.path == path)
    assert old.acknowledged
    encoded = (
        gzip.compress(payload) if compression == "gzip"
        else zstandard.ZstdCompressor().compress(payload)
    )
    compressed_path = path + suffix
    _write(guest, compressed_path, encoded)
    plain.unlink()
    with LokiServer() as server:
        second = hi.import_guest_logs(
            executor, _settings(server.url), store, KEY, name="pbs",
            files=[_wire(
                guest, compressed_path, compression=compression, profile="pbs", log_kind="task",
            )],
            sleep=lambda _s: None,
        )
    assert not second.failed
    assert server.requests == []
    assert store.source(KEY, old.source_id).acknowledged
    covered = next(item for item in second.covered_files if item["path"] == compressed_path)
    assert covered["sha256"] == hashlib.sha256(encoded).hexdigest()
