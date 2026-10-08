"""Real filesystem/transport and real-frame regressions for housekeeping_stream.

Every test here drives the real reader against a real node-transport tar (built
by :mod:`proxmox_fleet.housekeeping_io` through a scripted executor) or real
concatenated gzip/Zstandard bytes.  No network, no root and no guest is touched.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
import tracemalloc

import pytest
import zstandard

from proxmox_fleet import housekeeping_io as hio
from proxmox_fleet import housekeeping_stream as hs
from proxmox_fleet.runner import PrimitiveResult

CAPTURE_ID = "a" * 64 + "/" + "b" * 32
BLOB_NAME = "c" * 64 + ".blob"
LIVE_PATH = "/data/logs/access.log"
MANIFEST = hs.MANIFEST_MEMBER
RANGE = hs.RANGE_MEMBER


@pytest.mark.parametrize("compression", ["gzip", "zstd"])
def test_small_read_of_high_expansion_frame_keeps_memory_bounded(compression):
    payload = b"x" * (16 * 1024 * 1024)
    encoded = (
        gzip.compress(payload)
        if compression == "gzip"
        else zstandard.ZstdCompressor().compress(payload)
    )
    expected_digest = hashlib.sha256(payload).hexdigest()
    del payload
    tracemalloc.start()
    try:
        reader = hs.DecodedLogReader(io.BytesIO(encoded), compression)
        assert reader.read(1) == b"x"
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024
    while chunk := reader.read(64 * 1024):
        assert chunk == b"x" * len(chunk)
    assert reader.at_eof
    assert reader.sha256 == expected_digest


# --------------------------------------------------------------------------- #
# fixture builders
# --------------------------------------------------------------------------- #


class ScriptedExecutor:
    """Deterministic stand-in for the node snapshot transport."""

    def __init__(self, writer=None, *, fail: bool = False) -> None:
        self.calls = []
        self._writer = writer
        self._fail = fail

    def housekeeping_snapshot(self, lxc_id, *, files, destination):
        entry = dict(files[0])
        self.calls.append(
            {"lxc_id": lxc_id, "files": [entry], "destination": destination}
        )
        if self._fail:
            return PrimitiveResult(rc=1, failed=True, stderr="transport boom")
        assert self._writer is not None
        self._writer(destination, entry, len(self.calls) - 1)
        return PrimitiveResult(rc=0)


def _write(root: Path, rel: str, data: bytes, mode: int = 0o644) -> Path:
    target = root / rel.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(mode)
    return target


def _live_wire(path: str, st: os.stat_result, *, compression: str = "plain") -> dict:
    return {
        "path": path,
        "device": int(st.st_dev),
        "inode": int(st.st_ino),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "allocated_bytes": int(st.st_blocks * 512),
        "compression": compression,
        "profile": "npm",
        "log_kind": "application",
        "is_active": False,
    }


def _helper_writer(sysroot: Path | None, spool_root: Path | None = None):
    def write(destination, wire, index):
        hio.snapshot(
            {"files": [wire]},
            destination=destination,
            sysroot=None if sysroot is None else str(sysroot),
            spool_root=str(spool_root) if spool_root is not None else hio.SPOOL_ROOT,
        )

    return write


def _tar_write(destination, specs) -> None:
    with tarfile.open(destination, "w", format=tarfile.USTAR_FORMAT) as tar:
        for spec in specs:
            info = tarfile.TarInfo(spec["name"])
            if spec.get("kind") == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = spec.get("target", "target")
                tar.addfile(info)
                continue
            data = spec["data"]
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))


def _manifest_doc(wire, **over):
    entry = dict(wire)
    entry.setdefault("member", RANGE)
    entry.setdefault("source", "capture" if wire.get("blob_path") else "live")
    entry.update(over)
    return {"files": [entry]}


def _corrupt_writer(kind: str):
    def write(destination, wire, index):
        length = int(wire["length"])
        offset = int(wire["offset"])
        manifest = json.dumps(
            _manifest_doc(wire, offset=offset, length=length), separators=(",", ":")
        ).encode("utf-8")
        if kind == "missing":
            _tar_write(destination, [{"name": MANIFEST, "data": manifest}])
            return
        if kind == "extra":
            _tar_write(
                destination,
                [
                    {"name": MANIFEST, "data": manifest},
                    {"name": RANGE, "data": b"\x00" * length},
                    {"name": "extra.bin", "data": b"x"},
                ],
            )
            return
        if kind == "order":
            _tar_write(
                destination,
                [
                    {"name": RANGE, "data": b"\x00" * length},
                    {"name": MANIFEST, "data": manifest},
                ],
            )
            return
        if kind == "symlink":
            _tar_write(
                destination,
                [
                    {"name": MANIFEST, "data": manifest},
                    {"name": RANGE, "kind": "sym"},
                ],
            )
            return
        if kind == "traversal":
            _tar_write(
                destination,
                [
                    {"name": MANIFEST, "data": manifest},
                    {"name": "../evil.bin", "data": b"\x00" * length},
                ],
            )
            return
        if kind == "oversized":
            _tar_write(
                destination,
                [
                    {"name": MANIFEST, "data": manifest},
                    {"name": RANGE, "data": b"\x00" * (length + 4)},
                ],
            )
            return
        if kind == "truncated":
            _tar_write(
                destination,
                [
                    {"name": MANIFEST, "data": manifest},
                    {"name": RANGE, "data": b"\x00" * max(length - 1, 0)},
                ],
            )
            return
        if kind == "bad_manifest":
            _tar_write(
                destination,
                [
                    {"name": MANIFEST, "data": b"not json"},
                    {"name": RANGE, "data": b"\x00" * length},
                ],
            )
            return
        if kind == "manifest_offset":
            doc = _manifest_doc(wire, offset=offset + 1, length=length)
            _tar_write(
                destination,
                [
                    {
                        "name": MANIFEST,
                        "data": json.dumps(doc, separators=(",", ":")).encode(),
                    },
                    {"name": RANGE, "data": b"\x00" * length},
                ],
            )
            return
        if kind == "manifest_identity":
            doc = _manifest_doc(
                wire, offset=offset, length=length, device=int(wire.get("device", 0)) + 1
            )
            _tar_write(
                destination,
                [
                    {
                        "name": MANIFEST,
                        "data": json.dumps(doc, separators=(",", ":")).encode(),
                    },
                    {"name": RANGE, "data": b"\x00" * length},
                ],
            )
            return
        raise AssertionError(f"unknown corruption {kind!r}")

    return write


def _gzip_members(*chunks: bytes) -> bytes:
    out = io.BytesIO()
    for chunk in chunks:
        with gzip.GzipFile(fileobj=out, mode="wb") as handle:
            handle.write(chunk)
    return out.getvalue()


def _zstd_frames(*chunks: bytes) -> bytes:
    compressor = zstandard.ZstdCompressor()
    return b"".join(compressor.compress(chunk) for chunk in chunks)


def _reconstruct(fragments) -> bytes:
    out = bytearray()
    for fragment in fragments:
        entry = fragment.entry
        if "raw_base64" in entry:
            out += base64.b64decode(entry["raw_base64"])
        else:
            out += entry["_entry"].encode("utf-8")
        if entry["fragment_final"] and entry["terminated"]:
            out += b"\n"
    return bytes(out)


def _require_login_code(executor, wire, *, scratch, lxc_id="123"):
    reader = hs.RemoteLogReader(executor, lxc_id, wire, scratch_dir=str(scratch))
    with pytest.raises(hs.RemoteSourceError) as excinfo:
        reader.read()
    reader.close()
    return excinfo.value.code


# --------------------------------------------------------------------------- #
# RemoteLogReader: real helper transport
# --------------------------------------------------------------------------- #


def test_remote_reader_streams_live_range_and_digest(tmp_path: Path) -> None:
    root = tmp_path / "guest"
    payload = b"line one\nline two\n"
    log = _write(root, LIVE_PATH, payload)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    wire = _live_wire(LIVE_PATH, log.stat())
    executor = ScriptedExecutor(_helper_writer(root))

    with hs.RemoteLogReader(executor, "123", wire, scratch_dir=str(scratch)) as reader:
        assert reader.decoded_origin == 0
        assert reader.read() == payload
        assert reader.sha256 == hashlib.sha256(payload).hexdigest()
        assert reader.raw_bytes == len(payload)
        assert reader.finalized is True
        assert reader.stats["finalized"] is True

    assert [call["files"][0]["offset"] for call in executor.calls] == [0]
    assert list(scratch.iterdir()) == []
    assert executor.calls[0]["destination"].startswith(str(scratch))


def test_remote_reader_bounds_fetches_and_reassembles(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "guest"
    payload = b"0123456789AB"
    log = _write(root, LIVE_PATH, payload)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    wire = _live_wire(LIVE_PATH, log.stat())
    executor = ScriptedExecutor(_helper_writer(root))
    monkeypatch.setattr(hs, "MAX_FETCH_BYTES", 5)

    with hs.RemoteLogReader(executor, "123", wire, scratch_dir=str(scratch)) as reader:
        assert reader.read() == payload

    spans = [(call["files"][0]["offset"], call["files"][0]["length"]) for call in executor.calls]
    assert spans == [(0, 5), (5, 5), (10, 2)]
    assert all(length <= hs.MAX_FETCH_BYTES for _, length in spans)
    assert list(scratch.iterdir()) == []


def test_remote_reader_partial_window_uses_offset_origin(tmp_path: Path) -> None:
    root = tmp_path / "guest"
    payload = b"HEADERpayload-body-TAIL"
    log = _write(root, LIVE_PATH, payload)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    wire = _live_wire(LIVE_PATH, log.stat())
    wire["offset"] = 6
    wire["length"] = 12
    executor = ScriptedExecutor(_helper_writer(root))

    with hs.RemoteLogReader(executor, "123", wire, scratch_dir=str(scratch)) as reader:
        assert reader.decoded_origin == 6
        assert reader.read() == b"payload-body"
        assert reader.sha256 == hashlib.sha256(b"payload-body").hexdigest()


def test_remote_reader_captured_blob_uses_blob_identity(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    namespace, uuid = CAPTURE_ID.split("/")
    (spool / namespace / uuid).mkdir(parents=True)
    for path in (spool, spool / namespace, spool / namespace / uuid):
        path.chmod(0o700)
    payload = b"archive blob payload\n"
    blob = spool / namespace / uuid / BLOB_NAME
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = tmp_path / "guest"
    original = _write(root, "/data/logs/access.log.1", payload)
    wire = _live_wire("/data/logs/access.log.1", original.stat())
    wire.update(capture_id=CAPTURE_ID, blob_path=str(blob))
    captured = hio.capture(
        {"capture_id": CAPTURE_ID, "files": [wire]},
        sysroot=str(root),
        spool_root=str(spool),
    )
    blob_stat = blob.stat()
    wire.update(sha256=captured["files"][0]["sha256"], offset=0, length=len(payload))
    original.unlink()
    executor = ScriptedExecutor(_helper_writer(None, spool))

    with hs.RemoteLogReader(executor, "123", wire, scratch_dir=str(scratch)) as reader:
        assert reader.read() == payload
        assert reader.sha256 == hashlib.sha256(payload).hexdigest()
    assert executor.calls[0]["files"][0]["offset"] == 0
    assert wire["inode"] != int(blob_stat.st_ino)


# --------------------------------------------------------------------------- #
# RemoteLogReader: strict tar/member/metadata checks
# --------------------------------------------------------------------------- #


def _small_live_wire(tmp_path: Path, data: bytes = b"0123456789AB"):
    root = tmp_path / "guest"
    log = _write(root, LIVE_PATH, data)
    return root, _live_wire(LIVE_PATH, log.stat()), log.stat()


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("missing", "missing_member"),
        ("extra", "unexpected_member"),
        ("order", "unexpected_member"),
        ("symlink", "symlink"),
        ("traversal", "traversal"),
        ("oversized", "oversized"),
        ("truncated", "truncated"),
        ("bad_manifest", "conflict"),
        ("manifest_offset", "conflict"),
        ("manifest_identity", "conflict"),
    ],
)
def test_remote_reader_rejects_bad_tars(tmp_path: Path, kind: str, code: str) -> None:
    root, wire, _st = _small_live_wire(tmp_path)
    del root
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    executor = ScriptedExecutor(_corrupt_writer(kind))
    assert _require_login_code(executor, wire, scratch=scratch) == code
    assert list(scratch.iterdir()) == []


def test_remote_reader_rejects_oversized_tar_file(tmp_path: Path, monkeypatch) -> None:
    _root, wire, _st = _small_live_wire(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(hs, "_TAR_OVERHEAD", 32)

    def write(destination, wire_entry, index):
        _tar_write(
            destination,
            [
                {"name": MANIFEST, "data": b"x" * 200},
                {"name": RANGE, "data": b"y" * 200},
            ],
        )

    assert _require_login_code(ScriptedExecutor(write), wire, scratch=scratch) == "oversized"


def test_remote_reader_transport_failure_is_source_error(tmp_path: Path) -> None:
    _root, wire, _st = _small_live_wire(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    executor = ScriptedExecutor(fail=True)
    assert _require_login_code(executor, wire, scratch=scratch) == "transport"
    assert list(scratch.iterdir()) == []


def test_remote_reader_scratch_reserve_blocks_before_fetch(tmp_path: Path, monkeypatch) -> None:
    _root, wire, _st = _small_live_wire(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    executor = ScriptedExecutor(_corrupt_writer("missing"))
    monkeypatch.setattr(hs, "MIN_SCRATCH_FREE", 1 << 62)
    with hs.RemoteLogReader(executor, "123", wire, scratch_dir=str(scratch)) as reader:
        with pytest.raises(hs.StreamScratchError):
            reader.read()
    assert executor.calls == []


def test_remote_reader_missing_scratch_dir_is_error(tmp_path: Path) -> None:
    _root, wire, _st = _small_live_wire(tmp_path)
    with pytest.raises(hs.StreamScratchError):
        hs.RemoteLogReader(ScriptedExecutor(), "123", wire, scratch_dir=str(tmp_path / "nope"))


def test_remote_reader_owns_scratch_dir_and_removes_it(tmp_path: Path) -> None:
    root, wire, _st = _small_live_wire(tmp_path)
    executor = ScriptedExecutor(_helper_writer(root))
    reader = hs.RemoteLogReader(executor, "123", wire)
    scratch = reader._scratch
    assert os.path.isdir(scratch)
    with reader:
        assert reader.read() == b"0123456789AB"
    assert not os.path.exists(scratch)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda w: w.pop("inode"), "conflict"),
        (lambda w: w.update(compression="brotli"), "unsupported"),
        (lambda w: w.pop("device"), "conflict"),
        (lambda w: w.update(path="data/logs/access.log"), "conflict"),
    ],
)
def test_remote_reader_wire_validation(tmp_path: Path, mutate, code: str) -> None:
    root, wire, _st = _small_live_wire(tmp_path)
    mutate(wire)
    executor = ScriptedExecutor(_helper_writer(root))
    with pytest.raises(hs.RemoteSourceError) as excinfo:
        hs.RemoteLogReader(executor, "123", wire, scratch_dir=str(tmp_path))
    assert excinfo.value.code == code
    assert executor.calls == []


def test_remote_reader_compressed_requires_zero_offset(tmp_path: Path) -> None:
    root, wire, _st = _small_live_wire(tmp_path)
    wire["compression"] = "gzip"
    wire["offset"] = 4
    wire["length"] = 4
    with pytest.raises(hs.RemoteSourceError) as excinfo:
        hs.RemoteLogReader(ScriptedExecutor(_helper_writer(root)), "123", wire, scratch_dir=str(tmp_path))
    assert excinfo.value.code == "conflict"


def test_remote_reader_blob_wire_validation(tmp_path: Path) -> None:
    wire = {
        "path": "/data/logs/access.log.1",
        "size": 10,
        "compression": "plain",
        "capture_id": CAPTURE_ID,
        "blob_path": "/var/tmp/fleet-log-import/x/y/z.blob",
        "offset": 0,
        "length": 10,
    }
    with pytest.raises(hs.RemoteSourceError) as excinfo:
        hs.RemoteLogReader(ScriptedExecutor(), "123", wire, scratch_dir=str(tmp_path))
    assert excinfo.value.code == "conflict"


# --------------------------------------------------------------------------- #
# DecodedLogReader: plain / concatenated gzip / concatenated zstd
# --------------------------------------------------------------------------- #


def test_decoded_reader_plain_and_stats() -> None:
    payload = b"alpha\nbeta\ngamma"
    with hs.DecodedLogReader(io.BytesIO(payload), "plain") as reader:
        assert reader.read(6) == b"alpha\n"
        assert reader.readline() == b"beta\n"
        assert reader.read() == b"gamma"
        assert reader.sha256 == hashlib.sha256(payload).hexdigest()
        assert reader.decoded_bytes == len(payload)
        assert reader.position == len(payload)
        assert reader.at_eof is True


def test_decoded_reader_concatenated_gzip_frames() -> None:
    frames = [b"first-frame-marker\n", b"second-frame-marker\n", b"third-marker"]
    blob = _gzip_members(*frames)
    expected = b"".join(frames)
    with hs.DecodedLogReader(io.BytesIO(blob), "gzip") as reader:
        assert reader.read() == expected
        assert reader.sha256 == hashlib.sha256(expected).hexdigest()
        assert reader.decoded_bytes == len(expected)


def test_decoded_reader_concatenated_zstd_frames() -> None:
    frames = [b"zstd-one\n", b"zstd-two\n", b"zstd-three"]
    blob = _zstd_frames(*frames)
    expected = b"".join(frames)
    with hs.DecodedLogReader(io.BytesIO(blob), "zstd") as reader:
        assert reader.read() == expected
        assert reader.sha256 == hashlib.sha256(expected).hexdigest()
        assert reader.decoded_bytes == len(expected)


def test_decoded_reader_readline_over_gzip() -> None:
    blob = _gzip_members(b"one\ntwo\nthree")
    with hs.DecodedLogReader(io.BytesIO(blob), "gzip") as reader:
        assert reader.readline() == b"one\n"
        assert reader.readline(2) == b"tw"
        assert reader.read() == b"o\nthree"


@pytest.mark.parametrize("compression", ["gzip", "zstd"])
def test_decoded_reader_rejects_truncation_and_garbage(compression: str) -> None:
    payload = b"abcdef-abcdef-abcdef-abcdef"
    if compression == "gzip":
        good = _gzip_members(payload)
    else:
        good = _zstd_frames(payload)
    truncated = good[:-3]
    garbage = good + b"\xff\xff\xff\xff"
    with pytest.raises(hs.DecodeError) as truncated_error:
        with hs.DecodedLogReader(io.BytesIO(truncated), compression) as reader:
            reader.read()
    assert truncated_error.value.code == "truncated"
    with pytest.raises(hs.DecodeError) as garbage_error:
        with hs.DecodedLogReader(io.BytesIO(garbage), compression) as reader:
            reader.read()
    assert garbage_error.value.code == "corrupt"


def test_decoded_reader_rejects_unknown_compression() -> None:
    with pytest.raises(hs.DecodeError) as excinfo:
        hs.DecodedLogReader(io.BytesIO(b"x"), "lz4")
    assert excinfo.value.code == "unsupported"


# --------------------------------------------------------------------------- #
# iter_log_fragments: framing, offsets, continuation
# --------------------------------------------------------------------------- #


def _fragments(data: bytes, *, compression="plain", **kwargs):
    reader = hs.DecodedLogReader(io.BytesIO(data), compression)
    return reader, list(hs.iter_log_fragments(reader, filename=LIVE_PATH, source_id="s1", **kwargs))


def test_fragments_basic_lines_offsets_and_empty_lines() -> None:
    data = b"alpha\n\nbeta\n"
    reader, fragments = _fragments(data)
    assert "".join(f.entry["_entry"] for f in fragments) == "alpha" + "" + "beta"
    assert [(f.entry["offset"], f.next_offset) for f in fragments] == [(0, 6), (6, 7), (7, 12)]
    assert [f.entry["line_number"] for f in fragments] == [1, 2, 3]
    assert [f.next_line_number for f in fragments] == [2, 3, 4]
    assert all(f.entry["fragment_final"] and f.entry["terminated"] for f in fragments)
    assert [f.next_fragment_index for f in fragments] == [0, 0, 0]
    assert fragments[-1].decoded_bytes == len(data)
    assert _reconstruct(fragments) == data
    assert reader.sha256 == hashlib.sha256(data).hexdigest()


def test_fragments_long_line_splits_utf8_safe() -> None:
    line = ("\u00e9" * 9 + "x" * 30).encode("utf-8") + b"\n"
    data = b"head\n" + line
    _reader, fragments = _fragments(data, max_raw=8)
    payloads = [f.entry for f in fragments]
    # Every raw fragment respects the bound and never cuts a valid UTF-8 char.
    for fragment in fragments:
        raw = base64.b64decode(fragment.entry["raw_base64"]) if "raw_base64" in fragment.entry else fragment.entry["_entry"].encode()
        assert len(raw) <= 8
        raw.decode("utf-8")  # must be valid UTF-8 (no mid-character split)
    split = [f for f in fragments if f.entry["line_number"] == 2]
    assert len(split) > 1
    assert [f.entry["fragment_index"] for f in split] == list(range(len(split)))
    assert [f.entry["fragment_final"] for f in split] == [False] * (len(split) - 1) + [True]
    assert all(f.entry["terminated"] is False for f in split[:-1])
    assert split[-1].entry["terminated"] is True
    assert _reconstruct(fragments) == data
    assert payloads[-1]["line_number"] == 2


def test_fragments_non_utf8_bytes_round_trip() -> None:
    data = b"ok:" + b"\xff\xfe\x80raw" + b"\nnext\n"
    _reader, fragments = _fragments(data)
    assert len(fragments) == 2
    first = fragments[0].entry
    assert "raw_base64" in first
    assert base64.b64decode(first["raw_base64"]) == b"ok:" + b"\xff\xfe\x80raw"
    assert "\ufffd" in first["_entry"]
    assert _reconstruct(fragments) == data


def test_fragments_old_text_preserved_at_import_time() -> None:
    data = b"2019-01-02 03:04:05 legacy marker stays verbatim\n"
    _reader, fragments = _fragments(data)
    assert fragments[0].entry["_entry"] == "2019-01-02 03:04:05 legacy marker stays verbatim"
    assert _reconstruct(fragments) == data


def test_fragments_closed_unterminated_line_is_complete() -> None:
    data = b"complete\nunterminated tail"
    _reader, fragments = _fragments(data, active_prefix=False)
    tail = fragments[-1]
    assert tail.entry["_entry"] == "unterminated tail"
    assert tail.entry["fragment_final"] is True
    assert tail.entry["terminated"] is False
    assert "partial" not in tail.entry
    assert tail.next_line_number == 3
    assert tail.next_fragment_index == 0
    assert _reconstruct(fragments) == data


def test_fragments_active_prefix_partial_resumes_same_line() -> None:
    prefix = b"first\npartial active line"
    source_id = "npm:/data/logs/access.log"
    reader1 = hs.DecodedLogReader(io.BytesIO(prefix), "plain")
    part1 = list(
        hs.iter_log_fragments(
            reader1,
            filename=LIVE_PATH,
            source_id=source_id,
            active_prefix=True,
        )
    )
    partial = part1[-1]
    assert partial.entry["_entry"] == "partial active line"
    assert partial.entry["partial"] is True
    assert partial.entry["fragment_final"] is True
    assert partial.next_line_number == 2
    assert partial.next_fragment_index == 1
    assert partial.next_offset == len(prefix)

    combined = prefix + b" resumed suffix\nsecond line\n"
    reader2 = hs.DecodedLogReader(io.BytesIO(combined), "plain")
    part2 = list(
        hs.iter_log_fragments(
            reader2,
            filename=LIVE_PATH,
            source_id=source_id,
            start_offset=partial.next_offset,
            line_number=partial.next_line_number,
            fragment_index=partial.next_fragment_index,
            active_prefix=False,
        )
    )
    assert part2[0].entry["line_number"] == 2
    assert part2[0].entry["fragment_index"] == 1
    assert part2[0].entry["terminated"] is True
    assert part2[1].entry["line_number"] == 3
    assert _reconstruct(part1) + _reconstruct(part2) == combined


def test_fragments_resume_compressed_skips_acknowledged_bytes() -> None:
    payload = b"".join(b"record-%04d\n" % index for index in range(400))
    blob = _gzip_members(payload[:2000], payload[2000:])
    full_reader = hs.DecodedLogReader(io.BytesIO(blob), "gzip")
    full = list(hs.iter_log_fragments(full_reader, filename=LIVE_PATH, source_id="s1"))
    assert _reconstruct(full) == payload

    resume_point = full[199]
    resume_reader = hs.DecodedLogReader(io.BytesIO(blob), "gzip")
    resumed = list(
        hs.iter_log_fragments(
            resume_reader,
            filename=LIVE_PATH,
            source_id="s1",
            start_offset=resume_point.next_offset,
            line_number=resume_point.next_line_number,
            fragment_index=resume_point.next_fragment_index,
        )
    )
    assert [f.entry for f in resumed] == [f.entry for f in full[200:]]
    # Digest still covers the whole decoded stream even though the prefix was
    # discarded rather than emitted.
    assert resume_reader.sha256 == hashlib.sha256(payload).hexdigest()
    assert resume_reader.decoded_bytes == len(payload)


def test_fragments_start_offset_before_stream_is_error() -> None:
    reader = hs.DecodedLogReader(io.BytesIO(b"abcdef\n"), "plain")
    assert reader.read(4) == b"abcd"
    with pytest.raises(hs.HousekeepingStreamError) as excinfo:
        list(hs.iter_log_fragments(reader, filename=LIVE_PATH, source_id="s", start_offset=2))
    assert excinfo.value.code == "offset"


def test_fragments_start_offset_beyond_stream_is_truncated() -> None:
    reader = hs.DecodedLogReader(io.BytesIO(b"abc\n"), "plain")
    with pytest.raises(hs.RemoteSourceError) as excinfo:
        list(hs.iter_log_fragments(reader, filename=LIVE_PATH, source_id="s", start_offset=9))
    assert excinfo.value.code == "truncated"


def test_fragments_enforce_entry_bound_by_shrinking() -> None:
    long_name = "/data/logs/" + "n" * 60000
    data = b"\x01" * 16384 + b"\n"
    reader = hs.DecodedLogReader(io.BytesIO(data), "plain")
    fragments = list(
        hs.iter_log_fragments(reader, filename=long_name, source_id="s")
    )
    assert len(fragments) > 1
    for fragment in fragments:
        assert len(hs.encode_entry(fragment.entry)) <= hs.MAX_ENTRY_BYTES
        raw = base64.b64decode(fragment.entry["raw_base64"]) if "raw_base64" in fragment.entry else fragment.entry["_entry"].encode()
        assert len(raw) <= hs.MAX_RAW_FRAGMENT
    assert fragments[-1].entry["fragment_final"] is True
    assert _reconstruct(fragments) == data


def test_fragments_metadata_alone_too_large_raises() -> None:
    reader = hs.DecodedLogReader(io.BytesIO(b"payload\n"), "plain")
    with pytest.raises(hs.FragmentEncodingError):
        list(
            hs.iter_log_fragments(
                reader, filename="/data/logs/" + "n" * 200000, source_id="s"
            )
        )


def test_fragments_max_raw_clamped_to_hard_ceiling() -> None:
    data = b"a" * 40 + b"\n"
    _reader, fragments = _fragments(data, max_raw=100000)
    assert len(fragments) == 1
    assert fragments[0].entry["_entry"] == "a" * 40


def test_fragment_bounds_constants() -> None:
    assert hs.MAX_FETCH_BYTES == 32 * 1024 * 1024
    assert hs.MIN_SCRATCH_FREE == 64 * 1024 * 1024
    assert hs.MAX_ENTRY_BYTES == 128 * 1024
    assert hs.MAX_RAW_FRAGMENT == 16 * 1024
    assert hs.encode_entry({"a": "b"}) == b'{"a":"b"}'
