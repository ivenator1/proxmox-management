"""Bounded raw/decoded/fragment stream readers for the housekeeping importer.

This module is the shared reader layer between the node transport helper
(:mod:`proxmox_fleet.housekeeping_io`) and the manager-side archive importer
(``housekeeping_import.py``).  It owns *bytes and framing only*: no age, budget,
HTTP, checkpoint or deletion policy lives here.

Public API
----------

``RemoteLogReader(executor, lxc_id, file_wire, *, scratch_dir=None)``
    Context-managed, bounded ``io.RawIOBase`` adapter over one guest log source.
    It fetches the source in tar ranges of at most :data:`MAX_FETCH_BYTES`
    (32 MiB) through ``executor.housekeeping_snapshot`` and serves the raw bytes
    as ``read(n)`` / ``readinto(b)``.  It *never* extracts the tar; members are
    read in place and the scratch tar is deleted after each fetch.  Before every
    fetch the manager scratch filesystem must expose at least
    :data:`MIN_SCRATCH_FREE` (64 MiB) free or :class:`StreamScratchError` is
    raised.

    ``file_wire`` is the exact wire record of the io contract's schema.  Two
    shapes are accepted:

    * *live* sources (no ``blob_path``) require ``path``, ``device``, ``inode``,
      ``size``, ``mtime_ns`` and ``compression``;
    * *captured-blob* sources require ``path``, ``capture_id``, ``blob_path``,
      ``sha256`` and ``compression``.

    ``offset`` (default ``0``) and ``length`` (default ``size - offset``) select
    the raw window; compressed sources require ``offset == 0`` because a
    compressed stream cannot be seeked into.  ``.decoded_origin`` is the
    absolute decoded offset of the first byte the reader will serve (``offset``
    for ``plain``, ``0`` for ``gzip``/``zstd``).

    Attributes after reading: ``.sha256`` (hex digest of the raw bytes served
    through EOF), ``.raw_bytes``, ``.finalized`` (``True`` once the window's EOF
    is reached), ``.stats``.  ``.drain()`` reads and discards the remainder so
    the raw digest reaches EOF even when the consumer stopped early.

``DecodedLogReader(raw, compression, *, origin_offset=None)``
    Context-managed decoded stream over ``read(n)`` / ``readline(size)``.
    Supports ``plain``, concatenated ``gzip`` frames and concatenated Zstandard
    frames; every frame must be complete (truncation or trailing garbage raises
    :class:`DecodeError`).  ``.sha256`` is the full decoded digest from the
    stream origin through EOF and ``.decoded_bytes`` the decoded bytes consumed;
    ``.position`` is the absolute decoded offset of the next byte to deliver.

``iter_log_fragments(decoded, *, filename, source_id, start_offset=0,
line_number=1, fragment_index=0, active_prefix=False, max_raw=16384)``
    Yields :class:`LogFragment` records.  ``start_offset`` is the absolute
    decoded offset at which reading resumes: bytes between the stream's current
    ``position`` and ``start_offset`` are read and discarded (so the decoded
    digest still covers the whole stream from its origin).  Newline terminators
    are excluded from fragment payloads but counted by ``next_offset``; the
    ``terminated`` entry flag plus ``next_offset`` reconstruct them exactly.

    Fragment payloads are UTF-8-safely split into at most ``max_raw``
    (<= :data:`MAX_RAW_FRAGMENT`) raw bytes; when a payload is not valid UTF-8
    the entry additionally carries ``raw_base64`` with the exact bytes.
    ``encode_entry(entry)`` is the exact bounded encoder (compact JSON, UTF-8)
    and every entry is guaranteed <= :data:`MAX_ENTRY_BYTES` (128 KiB): the
    reader shrinks a fragment's payload rather than drop log bytes, and raises
    :class:`FragmentEncodingError` only when the shared metadata alone cannot
    fit.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import uuid
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple, Type

import zstandard

from proxmox_fleet.executor import Executor

__all__ = [
    "MAX_FETCH_BYTES",
    "MIN_SCRATCH_FREE",
    "MAX_ENTRY_BYTES",
    "MAX_RAW_FRAGMENT",
    "RANGE_MEMBER",
    "MANIFEST_MEMBER",
    "HousekeepingStreamError",
    "RemoteSourceError",
    "StreamScratchError",
    "DecodeError",
    "FragmentEncodingError",
    "LogFragment",
    "RemoteLogReader",
    "DecodedLogReader",
    "iter_log_fragments",
    "encode_entry",
]

#: Largest raw byte budget requested from the node in one snapshot fetch.
MAX_FETCH_BYTES = 32 * 1024 * 1024
#: Manager scratch must keep this much free before a bounded fetch.
MIN_SCRATCH_FREE = 64 * 1024 * 1024
#: Hard ceiling for one encoder-produced inner log entry.
MAX_ENTRY_BYTES = 128 * 1024
#: Hard ceiling for one raw fragment payload.
MAX_RAW_FRAGMENT = 16 * 1024

MANIFEST_MEMBER = "manifest.json"
RANGE_MEMBER = "ranges/000000.bin"

_READ_CHUNK = 1024 * 1024
_MANIFEST_LIMIT = 64 * 1024
_TAR_OVERHEAD = 1024 * 1024
_COMPRESSIONS = ("plain", "gzip", "zstd")
#: gzip members keep the gzip header/trailer window (zlib wbits 16 + MAX_WBITS).
_GZIP_WBITS = 16 + zlib.MAX_WBITS


def _gzip_frame() -> Any:
    """Create a fresh gzip decompressor for one concatenated member."""
    return zlib.decompressobj(_GZIP_WBITS)


def _zstd_frame() -> Any:
    """Create a fresh Zstandard decompressor object for one frame."""
    return zstandard.ZstdDecompressor().decompressobj()


class HousekeepingStreamError(Exception):
    """Base error carrying a bounded message, a stable ``code`` and ``detail``."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "error",
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail: Dict[str, Any] = dict(detail or {})


class RemoteSourceError(HousekeepingStreamError):
    """The remote source or its bounded snapshot transport failed validation.

    ``code`` is one of ``conflict`` (identity/truncation/mismatch), ``transport``
    (the node transport failed), ``truncated``, ``missing_member``,
    ``unexpected_member``, ``traversal``, ``symlink``, ``oversized`` or
    ``unsupported``.  Callers treat any ``RemoteSourceError`` as a blocked
    source that requires re-probe rather than progress.
    """


class StreamScratchError(HousekeepingStreamError):
    """Manager scratch space is missing or below the bounded-fetch reserve."""


class DecodeError(HousekeepingStreamError):
    """A compressed stream is corrupt, truncated or has unsupported framing."""


class FragmentEncodingError(HousekeepingStreamError):
    """A single entry cannot fit the bounded encoding even after shrinking."""


@dataclass(frozen=True)
class LogFragment:
    """One bounded entry plus its continuation cursor."""

    entry: Dict[str, Any]
    next_offset: int
    next_line_number: int
    next_fragment_index: int
    decoded_bytes: int


def encode_entry(entry: Mapping[str, Any]) -> bytes:
    """Encode one inner log entry exactly as the bounded importer does."""
    return json.dumps(entry, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _utf8_safe_end(buf: bytes, end: int) -> int:
    """Back *end* up so ``buf[:end]`` does not cut a valid multi-byte sequence."""
    if end <= 0:
        return 0
    if end >= len(buf):
        return len(buf)
    index = end
    while index > 0 and (buf[index - 1] & 0xC0) == 0x80:
        index -= 1
    if index == 0:
        return end
    lead = buf[index - 1]
    if lead < 0x80:
        need = 1
    elif lead < 0xE0:
        need = 2
    elif lead < 0xF0:
        need = 3
    else:
        need = 4
    if index - 1 + need > end:
        return index - 1
    return end


def _required_int(wire: Mapping[str, Any], key: str) -> int:
    value = wire.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RemoteSourceError(
            f"file wire is missing a valid non-negative {key!r}",
            code="conflict",
            detail={"key": key, "path": wire.get("path")},
        )
    return value


def _absolute_safe_path(path: Any) -> bool:
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    return ".." not in path.split("/")


class RemoteLogReader(io.RawIOBase):
    """Bounded raw byte stream for one guest log source (see module docstring)."""

    def __init__(
        self,
        executor: Executor,
        lxc_id: str,
        file_wire: Mapping[str, Any],
        *,
        scratch_dir: Optional[str] = None,
    ) -> None:
        super().__init__()
        # Cleanup state is initialised first so a construction failure cannot
        # trip __del__/close on a partially built reader.
        self._scratch = ""
        self._owned_scratch = False
        self._member: Optional[Any] = None
        self._tar: Optional[tarfile.TarFile] = None
        self._dest: Optional[str] = None
        self._fetch_remaining = 0
        self._at_eof = True
        self._delivered = 0
        self._digest = hashlib.sha256()
        self._executor = executor
        self._lxc_id = str(lxc_id)
        self._wire: Dict[str, Any] = dict(file_wire)
        self._scratch, self._owned_scratch = self._resolve_scratch(scratch_dir)

        compression = self._wire.get("compression")
        if compression not in _COMPRESSIONS:
            raise RemoteSourceError(
                "file wire compression must be plain, gzip or zstd",
                code="unsupported",
                detail={"path": self._wire.get("path"), "compression": compression},
            )
        self._compression = str(compression)
        if not _absolute_safe_path(self._wire.get("path")):
            raise RemoteSourceError(
                "file wire path must be absolute and traversal-free",
                code="conflict",
                detail={"path": self._wire.get("path")},
            )

        self._blob = bool(self._wire.get("blob_path"))
        size = _required_int(self._wire, "size")
        if self._blob:
            self._validate_blob_wire(size)
            offset = _required_int(self._wire, "offset")
            length = _required_int(self._wire, "length")
        else:
            self._validate_live_wire()
            offset = 0 if self._wire.get("offset") is None else _required_int(self._wire, "offset")
            length = size - offset if self._wire.get("length") is None else _required_int(self._wire, "length")
        if offset > size:
            raise RemoteSourceError(
                "file wire offset is beyond the recorded size",
                code="conflict",
                detail={"path": self._wire.get("path"), "offset": offset, "size": size},
            )
        if self._compression != "plain" and offset != 0:
            raise RemoteSourceError(
                "compressed sources must be read from offset 0",
                code="conflict",
                detail={"path": self._wire.get("path"), "offset": offset},
            )
        if not self._blob and offset + length > size:
            raise RemoteSourceError(
                "file wire range exceeds the recorded size",
                code="conflict",
                detail={"path": self._wire.get("path"), "offset": offset, "length": length},
            )
        self._offset = offset
        self._length = length
        self._decoded_origin = offset if self._compression == "plain" else 0

        self._at_eof = False

    # -- construction helpers ------------------------------------------------ #

    def _resolve_scratch(self, scratch_dir: Optional[str]) -> "tuple[str, bool]":
        if scratch_dir is None:
            return tempfile.mkdtemp(prefix="fleet-log-import-"), True
        path = os.path.abspath(str(scratch_dir))
        if not os.path.isdir(path):
            raise StreamScratchError(
                "scratch directory does not exist",
                code="scratch",
                detail={"scratch_dir": path},
            )
        return path, False

    def _validate_live_wire(self) -> None:
        _required_int(self._wire, "device")
        _required_int(self._wire, "inode")
        _required_int(self._wire, "mtime_ns")

    def _validate_blob_wire(self, size: int) -> None:
        del size  # the blob's own size comes from the node tar metadata
        capture_id = self._wire.get("capture_id")
        if not isinstance(capture_id, str) or not capture_id:
            raise RemoteSourceError(
                "captured blob wire requires capture_id",
                code="conflict",
                detail={"path": self._wire.get("path")},
            )
        if not _absolute_safe_path(self._wire.get("blob_path")):
            raise RemoteSourceError(
                "captured blob wire requires an absolute traversal-free blob_path",
                code="conflict",
                detail={"path": self._wire.get("path")},
            )
        digest = self._wire.get("sha256")
        if not isinstance(digest, str) or not digest:
            raise RemoteSourceError(
                "captured blob wire requires the recorded sha256",
                code="conflict",
                detail={"path": self._wire.get("path")},
            )

    # -- statistics ---------------------------------------------------------- #

    @property
    def compression(self) -> str:
        return self._compression

    @property
    def decoded_origin(self) -> int:
        return self._decoded_origin

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def length(self) -> int:
        return self._length

    @property
    def raw_bytes(self) -> int:
        return self._delivered

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    @property
    def finalized(self) -> bool:
        return self._at_eof

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "compression": self._compression,
            "origin_offset": self._decoded_origin,
            "offset": self._offset,
            "length": self._length,
            "raw_bytes": self._delivered,
            "sha256": self.sha256,
            "finalized": self._at_eof,
        }

    # -- fetching ------------------------------------------------------------ #

    def _check_scratch(self) -> None:
        try:
            statvfs = os.statvfs(self._scratch)
        except OSError as exc:
            raise StreamScratchError(
                "cannot inspect the scratch filesystem",
                code="scratch",
                detail={"scratch_dir": self._scratch, "reason": str(exc)},
            ) from exc
        free = statvfs.f_bavail * statvfs.f_frsize
        if free < MIN_SCRATCH_FREE:
            raise StreamScratchError(
                "insufficient manager scratch space for a bounded fetch",
                code="scratch",
                detail={"scratch_dir": self._scratch, "free_bytes": free},
            )

    def _safe_unlink(self, path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    def _finish_fetch(self) -> None:
        member, tar, dest = self._member, self._tar, self._dest
        self._member = None
        self._tar = None
        self._dest = None
        self._fetch_remaining = 0
        if member is not None:
            try:
                member.close()
            except OSError:
                pass
        if tar is not None:
            try:
                tar.close()
            except (OSError, tarfile.TarError):
                pass
        if dest is not None:
            self._safe_unlink(dest)

    def _open_fetch(self) -> None:
        self._finish_fetch()
        remaining = self._length - self._delivered
        if remaining <= 0:
            self._at_eof = True
            return
        current = self._offset + self._delivered
        size = min(MAX_FETCH_BYTES, remaining)
        self._check_scratch()
        destination = os.path.join(self._scratch, f"fleet-log-{uuid.uuid4().hex}.tar")
        request = dict(self._wire)
        request["offset"] = current
        request["length"] = size
        result = self._executor.housekeeping_snapshot(
            self._lxc_id, files=[request], destination=destination
        )
        if result is None or not result.ok:
            self._safe_unlink(destination)
            raise RemoteSourceError(
                "housekeeping snapshot transport failed",
                code="transport",
                detail={
                    "path": self._wire.get("path"),
                    "offset": current,
                    "length": size,
                },
            )
        try:
            tar, member = self._open_tar(destination, request, size)
        except BaseException:
            self._safe_unlink(destination)
            raise
        self._tar = tar
        self._member = member
        self._dest = destination
        self._fetch_remaining = size

    def _open_tar(
        self, destination: str, request: Mapping[str, Any], expected_length: int
    ) -> "tuple[tarfile.TarFile, Any]":
        if not os.path.isfile(destination):
            raise RemoteSourceError(
                "snapshot fetch did not produce the expected tar",
                code="transport",
                detail={"path": self._wire.get("path")},
            )
        if os.path.getsize(destination) > expected_length + _TAR_OVERHEAD:
            raise RemoteSourceError(
                "snapshot tar is larger than the bounded request",
                code="oversized",
                detail={"path": self._wire.get("path")},
            )
        try:
            tar = tarfile.open(destination, "r:")
        except (OSError, tarfile.TarError) as exc:
            raise RemoteSourceError(
                "snapshot tar is unreadable",
                code="conflict",
                detail={"path": self._wire.get("path"), "reason": str(exc)},
            ) from exc
        try:
            members = tar.getmembers()
            self._validate_members(members, request, expected_length)
            manifest_member = tar.getmember(MANIFEST_MEMBER)
            extracted = tar.extractfile(manifest_member)
            if extracted is None:
                raise RemoteSourceError(
                    "snapshot manifest member is not readable",
                    code="missing_member",
                    detail={"path": self._wire.get("path")},
                )
            with extracted:
                raw = extracted.read()
            self._validate_manifest(raw, request, expected_length)
            range_member = tar.getmember(RANGE_MEMBER)
            member_file = tar.extractfile(range_member)
            if member_file is None:
                raise RemoteSourceError(
                    "snapshot range member is not readable",
                    code="missing_member",
                    detail={"path": self._wire.get("path")},
                )
        except BaseException:
            try:
                tar.close()
            except (OSError, tarfile.TarError):
                pass
            raise
        return tar, member_file

    def _validate_members(
        self,
        members: "list[tarfile.TarInfo]",
        request: Mapping[str, Any],
        expected_length: int,
    ) -> None:
        names = [member.name for member in members]
        for member in members:
            if member.name.startswith("/") or ".." in member.name.split("/"):
                raise RemoteSourceError(
                    "snapshot tar member escapes the archive root",
                    code="traversal",
                    detail={"path": self._wire.get("path"), "member": member.name},
                )
            if member.issym() or member.islnk():
                raise RemoteSourceError(
                    "snapshot tar must not hold links",
                    code="symlink",
                    detail={"path": self._wire.get("path"), "member": member.name},
                )
            if not member.isreg():
                raise RemoteSourceError(
                    "snapshot tar must hold regular file members only",
                    code="unsupported",
                    detail={"path": self._wire.get("path"), "member": member.name},
                )
        if len(members) != 2 or set(names) != {MANIFEST_MEMBER, RANGE_MEMBER}:
            missing = {MANIFEST_MEMBER, RANGE_MEMBER} - set(names)
            if missing:
                raise RemoteSourceError(
                    "snapshot tar is missing expected members",
                    code="missing_member",
                    detail={"path": self._wire.get("path"), "missing": sorted(missing)},
                )
            raise RemoteSourceError(
                "snapshot tar holds unexpected members",
                code="unexpected_member",
                detail={"path": self._wire.get("path"), "members": sorted(names)},
            )
        if names[0] != MANIFEST_MEMBER:
            raise RemoteSourceError(
                "snapshot tar member order is not the documented order",
                code="unexpected_member",
                detail={"path": self._wire.get("path"), "members": names},
            )
        manifest_member = members[0]
        if manifest_member.size > _MANIFEST_LIMIT:
            raise RemoteSourceError(
                "snapshot manifest member is oversized",
                code="oversized",
                detail={"path": self._wire.get("path"), "size": manifest_member.size},
            )
        range_member = members[1]
        if range_member.size > expected_length:
            raise RemoteSourceError(
                "snapshot range member is oversized",
                code="oversized",
                detail={
                    "path": self._wire.get("path"),
                    "size": range_member.size,
                    "length": expected_length,
                },
            )
        if range_member.size < expected_length:
            raise RemoteSourceError(
                "snapshot range member is shorter than requested",
                code="truncated",
                detail={
                    "path": self._wire.get("path"),
                    "size": range_member.size,
                    "length": expected_length,
                },
            )

    def _validate_manifest(
        self, raw: bytes, request: Mapping[str, Any], expected_length: int
    ) -> None:
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteSourceError(
                "snapshot manifest is not valid JSON",
                code="conflict",
                detail={"path": self._wire.get("path"), "reason": str(exc)},
            ) from exc
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
            raise RemoteSourceError(
                "snapshot manifest must describe exactly one range",
                code="conflict",
                detail={"path": self._wire.get("path")},
            )
        entry = files[0]
        expected = {
            "member": RANGE_MEMBER,
            "offset": request["offset"],
            "length": expected_length,
            "path": self._wire.get("path"),
        }
        for key, value in expected.items():
            if entry.get(key) != value:
                raise RemoteSourceError(
                    "snapshot manifest entry does not match the request",
                    code="conflict",
                    detail={"path": self._wire.get("path"), "key": key},
                )
        if self._blob:
            checks = {
                "source": "capture",
                "capture_id": self._wire.get("capture_id"),
                "blob_path": self._wire.get("blob_path"),
            }
        else:
            checks = {
                "source": "live",
                "device": self._wire.get("device"),
                "inode": self._wire.get("inode"),
            }
        for key, value in checks.items():
            if entry.get(key) != value:
                raise RemoteSourceError(
                    "snapshot manifest identity does not match the wire",
                    code="conflict",
                    detail={"path": self._wire.get("path"), "key": key},
                )
        # For captured blobs the manifest reports the node blob's identity, not
        # the original source inode; only the blob's own length may be compared.
        blob_size = entry.get("size")
        if (
            isinstance(blob_size, bool)
            or not isinstance(blob_size, int)
            or blob_size < request["offset"] + expected_length
        ):
            raise RemoteSourceError(
                "snapshot manifest source is shorter than the requested range",
                code="truncated",
                detail={"path": self._wire.get("path")},
            )

    # -- BinaryIO surface ---------------------------------------------------- #

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed housekeeping stream")
        view = memoryview(b).cast("B")
        total = 0
        while total < len(view):
            if self._fetch_remaining <= 0:
                if self._at_eof:
                    break
                self._open_fetch()
                if self._at_eof or self._fetch_remaining <= 0:
                    break
            want = min(len(view) - total, self._fetch_remaining, _READ_CHUNK)
            if self._member is None:
                raise RemoteSourceError("snapshot range is unavailable", code="truncated")
            chunk = self._member.read(want)
            if not chunk:
                raise RemoteSourceError(
                    "snapshot range ended before the declared length",
                    code="truncated",
                    detail={"path": self._wire.get("path")},
                )
            view[total : total + len(chunk)] = chunk
            total += len(chunk)
            self._fetch_remaining -= len(chunk)
            self._delivered += len(chunk)
            self._digest.update(chunk)
            if self._fetch_remaining == 0:
                self._finish_fetch()
                if self._delivered >= self._length:
                    self._at_eof = True
                    self._verify_full_digest()
        return total

    def _verify_full_digest(self) -> None:
        """Verify a whole captured blob against its recorded provenance digest."""
        if not self._blob or self._offset != 0:
            return
        expected = self._wire.get("sha256")
        if self._length != self._wire.get("size"):
            return
        if self.sha256 != expected:
            raise RemoteSourceError(
                "captured blob digest does not match the recorded provenance",
                code="conflict",
                detail={"path": self._wire.get("path")},
            )

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            out = bytearray()
            while True:
                buffer = bytearray(_READ_CHUNK)
                count = self.readinto(buffer)
                if count <= 0:
                    break
                out += buffer[:count]
            return bytes(out)
        if size == 0:
            return b""
        buffer = bytearray(size)
        count = self.readinto(buffer)
        return bytes(buffer[:count])

    def drain(self) -> None:
        """Read and discard the remainder so the raw digest reaches EOF."""
        while not self._at_eof:
            if not self.read(_READ_CHUNK):
                break

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._finish_fetch()
        finally:
            if self._owned_scratch:
                shutil.rmtree(self._scratch, ignore_errors=True)
            super().close()


class DecodedLogReader:
    """Decoded byte stream with concatenated-frame support and full-stream digests."""

    def __init__(
        self,
        raw: Any,
        compression: str,
        *,
        origin_offset: Optional[int] = None,
    ) -> None:
        if compression not in _COMPRESSIONS:
            raise DecodeError(
                "compression must be plain, gzip or zstd",
                code="unsupported",
                detail={"compression": compression},
            )
        self._raw = raw
        self.compression = compression
        if origin_offset is None:
            origin_offset = int(getattr(raw, "decoded_origin", 0))
        self.origin_offset = int(origin_offset)
        self._plain = compression == "plain"
        self._make_frame: Optional[Callable[[], Any]]
        self._frame_errors: Tuple[Type[BaseException], ...]
        if compression == "gzip":
            self._make_frame = _gzip_frame
            self._frame_errors = (zlib.error,)
        elif compression == "zstd":
            self._make_frame = _zstd_frame
            self._frame_errors = (zstandard.ZstdError,)
        else:
            self._make_frame = None
            self._frame_errors = ()
        self._frame: Optional[Any] = None
        self._pending = bytearray()
        self._compressed = b""
        self._compressed_position = 0
        self._decoded_eof = False
        self._closed = False
        self._digest = hashlib.sha256()
        self.decoded_bytes = 0

    # -- statistics ---------------------------------------------------------- #

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()

    @property
    def position(self) -> int:
        return self.origin_offset + self.decoded_bytes - len(self._pending)

    @property
    def at_eof(self) -> bool:
        return self._decoded_eof and not self._pending

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "compression": self.compression,
            "origin_offset": self.origin_offset,
            "decoded_bytes": self.decoded_bytes,
            "sha256": self.sha256,
            "at_eof": self.at_eof,
        }

    # -- decoding ------------------------------------------------------------ #

    def _emit(self, data: bytes) -> None:
        if data:
            self._pending += data
            self.decoded_bytes += len(data)
            self._digest.update(data)

    def _drain_raw(self) -> None:
        drain = getattr(self._raw, "drain", None)
        if callable(drain):
            drain()

    def _pull_frame(self) -> bool:
        """Decode one bounded step, retaining strict end-of-frame validation."""
        if self._compressed_position == len(self._compressed):
            self._compressed = self._raw.read(64 * 1024)
            self._compressed_position = 0
            if not self._compressed:
                if self._frame is not None and not self._frame.eof:
                    raise DecodeError(
                        "compressed stream ended before the end-of-frame marker",
                        code="truncated",
                        detail={"compression": self.compression},
                    )
                self._frame = None
                self._decoded_eof = True
                self._drain_raw()
                return False
        if self._frame is None:
            assert self._make_frame is not None
            self._frame = self._make_frame()
        try:
            if self.compression == "gzip":
                # zlib exposes a true output limit and the unconsumed input.
                self._emit(self._frame.decompress(self._compressed, 64 * 1024))
                self._compressed = (
                    self._frame.unused_data
                    if self._frame.eof
                    else self._frame.unconsumed_tail
                )
                self._compressed_position = 0
            else:
                # zstandard.decompressobj has no output limit. Feed one byte:
                # completing a Zstandard block can emit at most 128 KiB. Larger
                # input steps can complete arbitrarily many highly compressed
                # blocks and materialize the entire history in one allocation.
                position = self._compressed_position
                self._emit(self._frame.decompress(self._compressed[position:position + 1]))
                self._compressed_position += 1
            if self._frame.eof:
                self._frame = None
        except self._frame_errors as exc:
            raise DecodeError(
                "compressed stream is corrupt",
                code="corrupt",
                detail={"compression": self.compression, "reason": str(exc)},
            ) from exc
        return True

    def _produce(self, want: int) -> None:
        while len(self._pending) < want and not self._decoded_eof:
            if self._plain:
                data = self._raw.read(want - len(self._pending))
                if not data:
                    self._decoded_eof = True
                    self._drain_raw()
                    break
                self._emit(data)
                continue
            if not self._pull_frame():
                break

    # -- stream surface ------------------------------------------------------ #

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("I/O operation on closed decoded stream")
        if size is None or size < 0:
            while not self._decoded_eof:
                self._produce(len(self._pending) + 1)
            data = bytes(self._pending)
            self._pending.clear()
            return data
        if size == 0:
            return b""
        self._produce(size)
        data = bytes(self._pending[:size])
        del self._pending[:size]
        return data

    def readline(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("I/O operation on closed decoded stream")
        limit = None if size is None or size < 0 else size
        while True:
            search_end = len(self._pending) if limit is None else min(len(self._pending), limit)
            newline = self._pending.find(b"\n", 0, search_end)
            if newline != -1:
                end = newline + 1
                break
            if limit is not None and len(self._pending) >= limit:
                end = limit
                break
            if self._decoded_eof:
                end = len(self._pending)
                break
            target = len(self._pending) + 1
            if limit is not None:
                target = min(target, limit)
            self._produce(target)
        data = bytes(self._pending[:end])
        del self._pending[:end]
        return data

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "DecodedLogReader":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class _LineBuffer:
    """Bounded lookahead over a decoded stream (never buffers a whole line)."""

    def __init__(self, decoded: DecodedLogReader, max_raw: int) -> None:
        self._decoded = decoded
        self._max = max_raw
        self._buf = bytearray()
        self._eof = False

    def push_front(self, data: bytes) -> None:
        if data:
            self._buf[:0] = data

    def _fill(self, want: int) -> None:
        while len(self._buf) < want and not self._eof:
            chunk = self._decoded.read(want - len(self._buf))
            if not chunk:
                self._eof = True
                break
            self._buf += chunk

    def next_chunk(self) -> "tuple[bytes, bool, bool]":
        """Return ``(content, terminated, eof_final)`` for the next fragment."""
        self._fill(self._max + 1)
        if not self._buf:
            return b"", False, True
        newline = self._buf.find(b"\n", 0, self._max + 1)
        if newline != -1:
            content = bytes(self._buf[:newline])
            del self._buf[: newline + 1]
            return content, True, False
        if len(self._buf) > self._max:
            end = _utf8_safe_end(bytes(self._buf), self._max)
            if end < 1:
                end = 1
            content = bytes(self._buf[:end])
            del self._buf[:end]
            return content, False, False
        content = bytes(self._buf)
        del self._buf[:]
        return content, False, True


def _build_entry(
    payload: bytes,
    *,
    filename: str,
    source_id: str,
    line_number: int,
    fragment_index: int,
    fragment_final: bool,
    terminated: bool,
    partial: bool,
    offset: int,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "_entry": payload.decode("utf-8", errors="replace"),
        "filename": filename,
        "line_number": line_number,
        "fragment_index": fragment_index,
        "fragment_final": fragment_final,
        "terminated": bool(fragment_final and terminated),
        "offset": offset,
        "source_id": source_id,
    }
    if partial:
        entry["partial"] = True
    if not _is_utf8(payload):
        entry["raw_base64"] = base64.b64encode(payload).decode("ascii")
    return entry


def iter_log_fragments(
    decoded: DecodedLogReader,
    *,
    filename: str,
    source_id: str,
    start_offset: int = 0,
    line_number: int = 1,
    fragment_index: int = 0,
    active_prefix: bool = False,
    max_raw: int = MAX_RAW_FRAGMENT,
) -> Iterator[LogFragment]:
    """Yield bounded :class:`LogFragment` records from a decoded log stream."""
    if isinstance(max_raw, bool) or not isinstance(max_raw, int) or max_raw < 1:
        raise HousekeepingStreamError(
            "max_raw must be a positive integer", code="unsupported"
        )
    limit = min(max_raw, MAX_RAW_FRAGMENT)
    if not hasattr(decoded, "position") or not hasattr(decoded, "decoded_bytes"):
        raise HousekeepingStreamError(
            "decoded stream must expose position and decoded_bytes",
            code="unsupported",
        )

    skip = start_offset - decoded.position
    if skip < 0:
        raise HousekeepingStreamError(
            "start_offset precedes the decoded stream position",
            code="offset",
            detail={"start_offset": start_offset, "position": decoded.position},
        )
    while skip > 0:
        chunk = decoded.read(min(skip, _READ_CHUNK))
        if not chunk:
            raise RemoteSourceError(
                "decoded stream ended before the resume offset",
                code="truncated",
                detail={"start_offset": start_offset},
            )
        skip -= len(chunk)

    buffer = _LineBuffer(decoded, limit)
    offset = start_offset
    while True:
        content, terminated, eof_final = buffer.next_chunk()
        if not content and not terminated:
            break
        line_end = terminated or eof_final
        partial = bool(line_end and not terminated and active_prefix)
        payload_length = len(content)
        while True:
            payload = content[:payload_length]
            final = line_end and payload_length == len(content)
            entry = _build_entry(
                payload,
                filename=filename,
                source_id=source_id,
                line_number=line_number,
                fragment_index=fragment_index,
                fragment_final=final,
                terminated=terminated,
                partial=partial if final else False,
                offset=offset,
            )
            encoded = encode_entry(entry)
            if len(encoded) <= MAX_ENTRY_BYTES:
                break
            if payload_length <= 1:
                raise FragmentEncodingError(
                    "entry metadata alone exceeds the bounded encoding",
                    code="encoding",
                    detail={
                        "filename": filename,
                        "source_id": source_id,
                        "line_number": line_number,
                    },
                )
            ratio = MAX_ENTRY_BYTES / len(encoded)
            target = int(payload_length * ratio * 0.97)
            if target >= payload_length:
                target = payload_length - 1
            if target < 1:
                target = 1
            payload_length = _utf8_safe_end(content, target)
            if payload_length < 1:
                payload_length = 1
        final = line_end and payload_length == len(content)
        if not final:
            remainder = content[payload_length:]
            if terminated:
                remainder = remainder + b"\n"
            buffer.push_front(remainder)
        if final and terminated:
            next_offset = offset + payload_length + 1
            next_line_number = line_number + 1
            next_fragment_index = 0
        elif final and active_prefix:
            # Frozen immutable prefix of an active source: the line is partial
            # and a later appended suffix continues the same line.
            next_offset = offset + payload_length
            next_line_number = line_number
            next_fragment_index = fragment_index + 1
        elif final:
            # A closed file's unterminated final line is that file's last line.
            next_offset = offset + payload_length
            next_line_number = line_number + 1
            next_fragment_index = 0
        else:
            next_offset = offset + payload_length
            next_line_number = line_number
            next_fragment_index = fragment_index + 1
        yield LogFragment(
            entry=entry,
            next_offset=next_offset,
            next_line_number=next_line_number,
            next_fragment_index=next_fragment_index,
            decoded_bytes=decoded.decoded_bytes,
        )
        offset = next_offset
        line_number = next_line_number
        fragment_index = next_fragment_index
