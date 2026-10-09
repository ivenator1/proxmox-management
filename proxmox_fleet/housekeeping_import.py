"""Acknowledged, resumable import of retained NPM/PBS file logs into Loki.

This module owns the *import* slice of the log-housekeeping feature:

* freeze the initial manifest in SQLite and on the PVE node **before** the first
  byte is copied (``CheckpointStore.begin_capture`` + ``housekeeping_capture``);
* read frozen node-spool blobs or live closed prefixes through
  :mod:`proxmox_fleet.housekeeping_stream` (pooling the frozen initial-capture
  ranges into as few validated node transfers as possible, without ever
  crediting a source from prefetched bytes), reconstructing original raw bytes
  (including delimiters and non-UTF-8) as bounded archive fragments;
* build bounded Loki ``/loki/api/v1/push`` bodies, persist the exact body as a
  pending batch **before** upload, and advance progress **only** on HTTP 204;
* release acknowledged node-spool blobs with an exact, validated command.

Nothing here deletes guest files: pruning is the parent policy's job, gated on
the ``covered_files`` list this module returns.  Each entry is a typed
:class:`~proxmox_fleet.housekeeping_sources.CurrentCoverage` proof that the
source's *current* identity is fully archived (raw fingerprint/digest verified,
frozen prefix complete, not
active/current/control); a frozen-prefix acknowledgement alone never authorises
deletion.  No HTTP request may carry log content into runner logs or
notifications.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shlex
import time
import urllib.error
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from proxmox_fleet import http as _http
from proxmox_fleet import housekeeping_sources as sources
from proxmox_fleet.executor import Executor
from proxmox_fleet.housekeeping_checkpoint import (
    BlobState,
    CheckpointError,
    CheckpointStore,
    GuestKey,
    PendingBatch,
    ProgressDelta,
    PruneIntent,
    SourceIdentity,
    SourceRecord,
)
from proxmox_fleet.housekeeping_io import SPOOL_ROOT
from proxmox_fleet.housekeeping_stream import (
    DecodedLogReader,
    HousekeepingStreamError,
    RemoteLogReader,
    RemoteSnapshotPool,
    RemoteSourceError,
    encode_entry,
    iter_log_fragments,
)
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.orchestration import retry

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Loki push endpoint suffix appended to the configured base URL.
LOKI_PUSH_PATH = "/loki/api/v1/push"
#: Maximum encoded push body (matches the checkpoint pending-body limit).
MAX_BODY_BYTES = 512 * 1024
#: A pending batch older than this is re-timestamped before retrying.
STALE_PENDING_NS = 24 * 60 * 60 * 1_000_000_000
#: Total attempts (first try + retries) for a transient push failure.
PUSH_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 30.0
_MAX_RETRY_AFTER = 60.0
# Keep the complete node-shell command below Linux's per-argument limit.
_MAX_RELEASE_BLOBS = 128

DELIVERY_ARCHIVE = "archive"
JOB_LABEL = "lxc-file"
#: Typed failure code for a PBS task row an earlier build reassigned to another task.
_TASK_REASSIGNED_CODE = "task-identity-reassigned"
_APP_BY_PROFILE = {"npm": "nginxproxymanager", "pbs": "proxmox-backup"}
_VALID_COMPRESSION = ("plain", "gzip", "zstd")
_VALID_PROFILE = ("npm", "pbs")
_VALID_LOG_KIND = ("application", "task", "api", "task_index")
_CAPTURE_ID_RE = re.compile(r"[0-9a-f]{64}/[0-9a-f]{32}\Z")
_BLOB_NAME_RE = re.compile(r"[0-9a-f]{64}\.blob\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

_WIRE_REQUIRED: Tuple[Tuple[str, type], ...] = (
    ("path", str),
    ("device", int),
    ("inode", int),
    ("size", int),
    ("mtime_ns", int),
    ("allocated_bytes", int),
    ("compression", str),
    ("profile", str),
    ("log_kind", str),
    ("is_active", bool),
)


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class ImportResult:
    """Outcome of one acknowledged-import run for a single guest."""

    bytes_archived: int = 0
    pending: bool = False
    failed: bool = False
    warnings: List[str] = field(default_factory=list)
    covered_files: List[Dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Typed import concepts
#
# Two things the importer must never conflate are represented explicitly
# instead of as bare booleans/dict lookups:
#   * ``BatchProgress``/``PrefixCompletion`` -- what a single pushed body's
#     acknowledgement means (bytes durable vs. immutable prefix finished);
#   * the durable provenance/coverage invariants owned by
#     :mod:`proxmox_fleet.housekeeping_sources` (``SourceProvenance``,
#     ``CurrentCoverage`` and the generation/lineage predicates).
# --------------------------------------------------------------------------- #


class PrefixCompletion(str, Enum):
    """Whether one pushed body finishes a source's immutable frozen prefix.

    A live/current file keeps growing, so only the bytes frozen by the initial
    capture (or by the moment a rotated file was closed) can be ``COMPLETE``.
    """

    INCOMPLETE = "incomplete"
    COMPLETE = "complete"


@dataclass(frozen=True)
class BatchProgress:
    """Durable progress carried by exactly one saved Loki body for one source.

    HTTP 204 makes the body's bytes durable, but only a ``COMPLETE`` body may
    advance the initial-manifest gate: a streaming body's offset is retained
    while ``acknowledged`` stays False, so a partially-covered prefix can never
    look finished.  The typed value is reduced to the existing durable
    :class:`ProgressDelta` at the store boundary -- no extra flag or column.
    """

    source_id: str
    decoded_offset: int
    line_number: int
    fragment_index: int
    stream_ts_ns: int
    completion: PrefixCompletion = PrefixCompletion.INCOMPLETE
    digest: Optional[str] = None

    def to_delta(self) -> ProgressDelta:
        complete = self.completion is PrefixCompletion.COMPLETE
        return ProgressDelta(
            source_id=self.source_id,
            decoded_offset=self.decoded_offset,
            line_number=self.line_number,
            fragment_index=self.fragment_index,
            stream_ts_ns=self.stream_ts_ns,
            acknowledged=complete and self.digest is not None,
            digest=self.digest if complete else None,
        )


# --------------------------------------------------------------------------- #
# Deterministic identifiers
# --------------------------------------------------------------------------- #


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def capture_id_for(key: GuestKey) -> str:
    """Deterministic, resumable capture id for one guest (``<64hex>/<32hex>``)."""
    namespace = _sha256(f"{key.cluster}\0{key.node}\0{key.lxc_id}")
    unique = _sha256(f"{namespace}\0initial")[:32]
    return f"{namespace}/{unique}"


def base_source_id(profile: str, log_kind: str, path: str) -> str:
    return _sha256(f"{profile}|{log_kind}|{path}")[:32]


def blob_path_for(capture_id: str, source_id: str, *, spool_root: Optional[str] = None) -> str:
    root = SPOOL_ROOT if spool_root is None else spool_root
    token = _sha256(f"blob|{source_id}")
    return f"{root}/{capture_id}/{token}.blob"


def validate_capture_id(capture_id: str) -> None:
    if not isinstance(capture_id, str) or not _CAPTURE_ID_RE.match(capture_id):
        raise ValueError(f"invalid capture_id: {capture_id!r}")


# --------------------------------------------------------------------------- #
# Wire helpers
# --------------------------------------------------------------------------- #


def _normalize_files(files: Sequence[Dict[str, Any]], warnings: List[str]) -> List[Dict[str, Any]]:
    """Validate wire records; a malformed record never authorizes any action."""
    out: List[Dict[str, Any]] = []
    for raw in files:
        if not isinstance(raw, dict):
            warnings.append("housekeeping import: skipped malformed file record")
            continue
        ok = True
        for key, kind in _WIRE_REQUIRED:
            value = raw.get(key)
            if kind is int:
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    ok = False
                    break
            elif not isinstance(value, kind):
                ok = False
                break
        if ok and raw["compression"] not in _VALID_COMPRESSION:
            ok = False
        if ok and raw["profile"] not in _VALID_PROFILE:
            ok = False
        if ok and raw["log_kind"] not in _VALID_LOG_KIND:
            ok = False
        if not ok or not str(raw["path"]).startswith("/"):
            warnings.append("housekeeping import: skipped invalid file identity")
            continue
        out.append(dict(raw))
    return out


def _identity(wire: Dict[str, Any]) -> SourceIdentity:
    return SourceIdentity(
        path=wire["path"],
        device=wire["device"],
        inode=wire["inode"],
        size=wire["size"],
        mtime_ns=wire["mtime_ns"],
        allocated_bytes=wire["allocated_bytes"],
        compression=wire["compression"],
        profile=wire["profile"],
        log_kind=wire["log_kind"],
        is_active=bool(wire["is_active"]),
    )


def _archive_labels(key: GuestKey, name: str, wire: Dict[str, Any]) -> Dict[str, str]:
    return {
        "job": JOB_LABEL,
        "delivery": DELIVERY_ARCHIVE,
        "cluster": key.cluster,
        "node": key.node,
        "guest_id": key.lxc_id,
        "host": name,
        "app": _APP_BY_PROFILE.get(wire["profile"], "unknown"),
        "log_kind": wire["log_kind"],
    }


def _labels_json(labels: Dict[str, str]) -> bytes:
    return json.dumps(labels, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def _encode_value(timestamp_ns: int, line: bytes) -> bytes:
    return json.dumps(
        [str(timestamp_ns), line.decode("utf-8")],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _build_body(labels_json: bytes, values: Sequence[bytes]) -> bytes:
    return b'{"streams":[{"stream":' + labels_json + b',"values":[' + b",".join(values) + b"]}]}"


def _bounded(text: str, limit: int = 240) -> str:
    return " ".join(str(text).split())[:limit]


# --------------------------------------------------------------------------- #
# Bounded snapshot pool
# --------------------------------------------------------------------------- #


def _pool_eligible(read_wire: Optional[Dict[str, Any]]) -> bool:
    """A read wire may be pooled only when it is a frozen captured-blob range.

    Live/current sources are never pooled: the pool serves a range only on an
    exact ``(capture_id, blob_path, offset, length)`` hint match and falls back
    to the strict singleton reader otherwise, so passing the pool to a live read
    is a no-op.  Only the frozen initial-capture ranges are advertised as hints.
    """
    return (
        read_wire is not None
        and "blob_path" in read_wire
        and "capture_id" in read_wire
        and "offset" in read_wire
        and "length" in read_wire
    )


@contextlib.contextmanager
def _snapshot_pool(
    executor: Executor, lxc_id: str, wires: Sequence[Dict[str, Any]]
) -> "Iterator[Optional[RemoteSnapshotPool]]":
    """Open one lazily-fetched pool for the plan's eligible frozen ranges.

    The pool batches the advertised hints (up to its own 64-range / 32 MiB
    bounds) so one validated node transfer can feed several sources; a
    subsequent transport outage therefore cannot strand already-fetched bytes.
    The pool never acknowledges anything: each source still advances only on
    its own HTTP 204.  With no eligible wires the loop runs on the strict
    singleton reader (``pool=None``).
    """
    if not wires:
        yield None
        return
    with RemoteSnapshotPool(executor, lxc_id, list(wires)) as pool:
        yield pool


# --------------------------------------------------------------------------- #
# Node-spool release command (CaptureSafety manifest / released.json contract)
# --------------------------------------------------------------------------- #

_RELEASE_PROGRAM = r'''
import hashlib, json, os, re, stat, sys, time

BLOB_RE = re.compile(r"[0-9a-f]{64}\.blob\Z")
BLOB_JSON_RE = re.compile(r"[0-9a-f]{64}\.blob\.json\Z")
CAPTURE_RE = re.compile(r"[0-9a-f]{64}/[0-9a-f]{32}\Z")
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)


def _fail(msg):
    sys.stdout.write(json.dumps({"error": msg}, separators=(",", ":"), sort_keys=True) + "\n")
    raise SystemExit(1)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(os.path.dirname(path), _DIR_FLAGS)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _verify_dir(path):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        _fail("spool path is not a directory")
    if st.st_uid != os.geteuid() or (stat.S_IMODE(st.st_mode) & 0o077):
        _fail("spool path is not owned by the current user with no group/other access")


def _receipt_meta(capture_dir, capture_id, base, expected_digest):
    path = os.path.join(capture_dir, "receipts", base + ".json")
    try:
        with open(path, "rb") as handle:
            parsed = json.loads(handle.read().decode("utf-8"))
    except OSError:
        _fail("capture receipt missing")
    except ValueError:
        _fail("capture receipt corrupt")
    if not isinstance(parsed, dict) or parsed.get("capture_id") != capture_id:
        _fail("capture receipt mismatch")
    entry = parsed.get("entry")
    blob = parsed.get("blob")
    if not isinstance(entry, dict) or not isinstance(blob, dict):
        _fail("capture receipt malformed")
    canonical = json.dumps(
        {"capture_id": parsed["capture_id"], "entry": entry, "blob": blob},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if _sha256(canonical) != parsed.get("receipt_sha256"):
        _fail("capture receipt checksum mismatch")
    if entry.get("sha256") != expected_digest:
        _fail("capture receipt digest mismatch")
    fields = (blob.get("device"), blob.get("inode"), blob.get("size"), blob.get("mtime_ns"))
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in fields):
        _fail("capture receipt metadata malformed")
    return {"device": int(fields[0]), "inode": int(fields[1]), "size": int(fields[2]), "mtime_ns": int(fields[3])}


def _remove_dir(capture_dir):
    names = set(os.listdir(capture_dir))
    if not names <= {"manifest.json", "released.json", "receipts"}:
        return False
    receipt_names = []
    if "receipts" in names:
        receipts = os.path.join(capture_dir, "receipts")
        st = os.lstat(receipts)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return False
        for entry in os.listdir(receipts):
            if not BLOB_JSON_RE.match(entry):
                return False
            receipt_names.append(entry)
    for name in ("manifest.json", "released.json"):
        path = os.path.join(capture_dir, name)
        if os.path.exists(path):
            os.unlink(path)
    for entry in receipt_names:
        os.unlink(os.path.join(capture_dir, "receipts", entry))
    if "receipts" in names:
        os.rmdir(os.path.join(capture_dir, "receipts"))
    os.rmdir(capture_dir)
    return True


def main(argv):
    if len(argv) < 3:
        _fail("usage: release <spool_root> <capture_id> <blob_path> [blob_path...]")
    spool_root, capture_id = argv[0], argv[1]
    blob_paths = argv[2:]
    if not CAPTURE_RE.match(capture_id):
        _fail("invalid capture_id")
    namespace, unique = capture_id.split("/")
    capture_dir = os.path.join(spool_root, namespace, unique)
    for path in (spool_root, os.path.join(spool_root, namespace), capture_dir):
        try:
            _verify_dir(path)
        except OSError:
            _fail("spool path missing")
    manifest_path = os.path.join(capture_dir, "manifest.json")
    try:
        with open(manifest_path, "rb") as handle:
            manifest = json.loads(handle.read().decode("utf-8"))
    except OSError:
        _fail("capture manifest missing")
    except ValueError:
        _fail("capture manifest corrupt")
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(entries, list):
        _fail("capture manifest malformed")
    digest_by_base = {}
    for entry in entries:
        if not isinstance(entry, dict):
            _fail("capture manifest entry malformed")
        blob_path = entry.get("blob_path")
        digest = entry.get("sha256")
        if not isinstance(blob_path, str) or not isinstance(digest, str) or not BLOB_RE.match(os.path.basename(blob_path)):
            _fail("capture manifest entry malformed")
        digest_by_base[os.path.basename(blob_path)] = digest
    released_path = os.path.join(capture_dir, "released.json")
    released = {}
    if os.path.exists(released_path):
        try:
            with open(released_path, "rb") as handle:
                marker = json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError):
            _fail("released marker unreadable")
        if not isinstance(marker, dict) or marker.get("capture_id") != capture_id or not isinstance(marker.get("released"), dict):
            _fail("released marker mismatch")
        canonical = json.dumps(marker["released"], separators=(",", ":"), sort_keys=True).encode("utf-8")
        if _sha256(canonical) != marker.get("released_sha256"):
            _fail("released marker checksum mismatch")
        released = dict(marker["released"])
    validated = []
    for blob_path in blob_paths:
        if os.path.dirname(blob_path) != capture_dir:
            _fail("blob path is not an exact spool member")
        base = os.path.basename(blob_path)
        if not BLOB_RE.match(base) or base not in digest_by_base:
            _fail("blob is not declared in the capture manifest")
        meta = _receipt_meta(capture_dir, capture_id, base, digest_by_base[base])
        try:
            st = os.lstat(blob_path)
        except OSError:
            if base in released and released[base].get("sha256") == digest_by_base[base]:
                continue
            _fail("blob is missing")
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            _fail("blob is not a regular file")
        if st.st_uid != os.geteuid() or (stat.S_IMODE(st.st_mode) & 0o077):
            _fail("blob is not owned by the current user with no group/other access")
        observed = (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))
        if observed != (meta["device"], meta["inode"], meta["size"], meta["mtime_ns"]):
            _fail("capture blob changed since it was captured")
        validated.append((blob_path, base))
    now = time.time_ns()
    for _blob_path, base in validated:
        released[base] = {"sha256": digest_by_base[base], "released_ns": now}
    payload = {"capture_id": capture_id, "released": released}
    payload["released_sha256"] = _sha256(json.dumps(released, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    _atomic_write(released_path, json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    unlinked = []
    for blob_path, base in validated:
        try:
            os.unlink(blob_path)
        except FileNotFoundError:
            pass
        unlinked.append(base)
    removed = False
    if digest_by_base and set(digest_by_base) <= set(released):
        removed = _remove_dir(capture_dir)
    sys.stdout.write(
        json.dumps({"capture_id": capture_id, "released": sorted(unlinked), "removed_dir": removed}, separators=(",", ":"), sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def build_capture_release_command(
    capture_id: str,
    blob_paths: Sequence[str],
    *,
    spool_root: Optional[str] = None,
) -> str:
    """Build the exact, validated node-shell command that releases spool blobs.

    Only blobs living directly under ``<spool_root>/<capture_id>/`` with a
    ``<64hex>.blob`` basename are accepted; the generated program refuses any
    symlink/ownership/permutation mismatch and writes the cumulative
    ``released.json`` marker before unlinking.
    """
    validate_capture_id(capture_id)
    if not blob_paths:
        raise ValueError("no blob paths to release")
    root = SPOOL_ROOT if spool_root is None else spool_root
    prefix = f"{root}/{capture_id}/"
    for path in blob_paths:
        if not isinstance(path, str) or not path.startswith(prefix):
            raise ValueError(f"blob path outside capture spool: {path!r}")
        if not _BLOB_NAME_RE.match(path[len(prefix):]):
            raise ValueError(f"blob path is not an exact spool member: {path!r}")
    args = " ".join(shlex.quote(str(item)) for item in (root, capture_id, *blob_paths))
    return f"python3 -c {shlex.quote(_RELEASE_PROGRAM)} {args}"


# --------------------------------------------------------------------------- #
# HTTP push
# --------------------------------------------------------------------------- #


@dataclass
class _PushOutcome:
    status: int
    ok: bool
    retryable: bool
    detail: str


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def push_batch(
    push_url: str,
    body: bytes,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> _PushOutcome:
    """POST one bounded body; retry timeout/429/5xx at most ``PUSH_MAX_ATTEMPTS``.

    Other 4xx statuses return immediately.  The exact bytes are never modified
    and no log content is ever placed into the returned detail.
    """
    state: Dict[str, Any] = {"attempts": 0, "retry_after": None}
    last: Dict[str, _PushOutcome] = {}

    def attempt() -> _PushOutcome:
        state["attempts"] += 1
        retry_after: Optional[str] = None
        try:
            response = _http.request(
                push_url,
                method="POST",
                headers={"Content-Type": "application/json"},
                data=body,
                timeout=30.0,
            )
            status = int(response.status)
            retry_after = response.headers.get("Retry-After") if response.headers else None
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
        except (urllib.error.URLError, OSError) as exc:
            state["retry_after"] = None
            outcome = _PushOutcome(0, False, True, _bounded(f"loki transport {type(exc).__name__}"))
            last["outcome"] = outcome
            return outcome
        state["retry_after"] = retry_after
        if status == 204:
            outcome = _PushOutcome(204, True, False, "")
        elif status in (408, 429) or 500 <= status < 600:
            outcome = _PushOutcome(status, False, True, _bounded(f"loki status {status}"))
        else:
            outcome = _PushOutcome(status, False, False, _bounded(f"loki status {status}"))
        last["outcome"] = outcome
        return outcome

    def sleeper(delay: float) -> None:
        expo = min(_BACKOFF_BASE * (2 ** max(state["attempts"] - 1, 0)), _BACKOFF_MAX)
        retry_after = _parse_retry_after(state["retry_after"])
        sleep(min(retry_after, _MAX_RETRY_AFTER) if retry_after is not None else expo)

    try:
        return retry(
            attempt,
            retries=PUSH_MAX_ATTEMPTS - 1,
            delay=_BACKOFF_BASE,
            until=lambda outcome: outcome.ok or not outcome.retryable,
            exceptions=(),
            sleep=sleeper,
        )
    except RuntimeError:
        return last.get("outcome", _PushOutcome(0, False, True, "loki retry exhausted"))


# --------------------------------------------------------------------------- #
# Import state
# --------------------------------------------------------------------------- #


@dataclass
class _ImportState:
    key: GuestKey
    lxc_id: str
    name: str
    store: CheckpointStore
    push_url: str
    budget: int
    clock_ns: Callable[[], int]
    sleep: Callable[[float], None]
    warnings: List[str] = field(default_factory=list)
    bytes_archived: int = 0
    body_bytes: int = 0
    pending: bool = False
    failed: bool = False
    stopped: bool = False
    covered: List[Dict[str, Any]] = field(default_factory=list)
    _ts: int = 0

    def observe_ts(self, timestamp_ns: Optional[int]) -> None:
        if timestamp_ns and timestamp_ns > self._ts:
            self._ts = timestamp_ns

    def next_ts(self) -> int:
        self._ts = max(self._ts + 1, int(self.clock_ns()))
        return self._ts

    def budget_reached(self) -> bool:
        """The per-run cap applies to both decoded bytes and encoded push bodies."""
        return self.bytes_archived >= self.budget or self.body_bytes >= self.budget

    def add_covered(self, wire: Dict[str, Any], record: SourceRecord) -> None:
        coverage = sources.current_coverage(record, wire)
        if coverage is None:
            return
        if any(entry.get("source_id") == coverage.source_id for entry in self.covered):
            return
        self.covered.append(coverage.to_wire())


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


@dataclass
class _FilePlan:
    source_id: str
    wire: Optional[Dict[str, Any]]
    identity: SourceIdentity
    read_wire: Optional[Dict[str, Any]]
    raw_size: int
    start_offset: int
    line_number: int
    fragment_index: int
    active_prefix: bool
    in_initial_manifest: bool
    complete_for_current: bool = False
    lineage_predecessor: Optional[SourceRecord] = None
    verify_raw_size: int = 0
    verify_raw_sha256: Optional[str] = None


def _live_read_wire(wire: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "path": wire["path"],
        "device": wire["device"],
        "inode": wire["inode"],
        "size": wire["size"],
        "mtime_ns": wire["mtime_ns"],
        "allocated_bytes": wire["allocated_bytes"],
        "compression": wire["compression"],
        "profile": wire["profile"],
        "log_kind": wire["log_kind"],
        "is_active": bool(wire["is_active"]),
    }


def _blob_read_wire(blob: BlobState, record: Optional[SourceRecord]) -> Dict[str, Any]:
    return {
        "path": record.path if record is not None else blob.source_path,
        "device": blob.device,
        "inode": blob.inode,
        "size": blob.high_water_size,
        "mtime_ns": record.mtime_ns if record is not None else 0,
        "allocated_bytes": record.allocated_bytes if record is not None else 0,
        "compression": record.compression if record is not None else blob.compression,
        "profile": record.profile if record is not None else "npm",
        "log_kind": record.log_kind if record is not None else "application",
        "is_active": record.is_active if record is not None else False,
        "capture_id": blob.capture_id,
        "blob_path": blob.blob_path,
        "sha256": blob.sha256,
        "offset": 0,
        "length": blob.high_water_size,
    }


def _select_read_wire(
    record: SourceRecord,
    blob: Optional[BlobState],
    wire: Optional[Dict[str, Any]],
    complete_for_current: bool,
) -> Optional[Dict[str, Any]]:
    if complete_for_current:
        return None
    if blob is not None and blob.captured and blob.sha256:
        if record.capture_id == blob.capture_id:
            provenance = sources.SourceProvenance.parse(record.provenance)
            if sources.is_prefix_complete(record) and wire is not None and wire["is_active"]:
                return None
            if (
                sources.is_prefix_complete(record)
                and wire is not None
                and wire["compression"] == "plain"
                and wire["size"] > provenance.complete_size
                and not wire["is_active"]
            ):
                return _live_read_wire(wire)
            if sources.is_prefix_complete(record) and wire is None:
                return None
            return _blob_read_wire(blob, record)
    if wire is None:
        return None
    return _live_read_wire(wire)


def _read_wire_active(read_wire: Optional[Dict[str, Any]], record: Optional[SourceRecord]) -> bool:
    if read_wire is not None and "blob_path" in read_wire:
        if record is not None:
            raw = record.provenance
            if isinstance(raw, Mapping) and "active_at_capture" in raw:
                return sources.SourceProvenance.parse(raw).active_at_capture
            return bool(record.is_active)
        return False
    return bool(read_wire and read_wire.get("is_active"))


def _build_plan(
    store: CheckpointStore,
    key: GuestKey,
    wire: Optional[Dict[str, Any]],
    record: Optional[SourceRecord],
    blob: Optional[BlobState],
    manifest_wire: Optional[Dict[str, Any]],
    *,
    existing_ids: Sequence[str],
    base_id: str,
    current_paths: Set[str],
) -> _FilePlan:
    reference = wire or manifest_wire
    assert reference is not None
    same = record is not None and sources.same_generation(record, wire, current_paths)
    verify_raw_size = 0
    verify_raw_sha256 = None
    if same and record is not None and wire is not None and record.digest:
        provenance = sources.SourceProvenance.parse(record.provenance)
        if (
            record.path != wire["path"]
            or wire["size"] > provenance.complete_size
            or provenance.coverage_conflict
            or not sources.is_prefix_complete(record)
        ):
            verify_raw_size = provenance.verified_raw_size or provenance.complete_size or record.size
            verify_raw_sha256 = record.digest
    if not same:
        fresh_manifest_source = record is None
        if fresh_manifest_source:
            source_id = base_id
            generation = 1
        else:
            source_id, generation = sources.next_generation(existing_ids, base_id)
        # A fresh generation of an *existing* source must never inherit the
        # previous generation's frozen blob — that blob belongs to the old inode.
        attached = blob if (blob is not None and fresh_manifest_source) else None
        fresh_provenance = sources.SourceProvenance.fresh(
            reference,
            attached.capture_id if attached is not None else None,
            generation=generation,
        ).as_dict()
        store.record_source(
            key,
            source_id,
            _identity(reference),
            provenance=fresh_provenance,
            in_initial_manifest=attached is not None,
            capture_id=attached.capture_id if attached is not None else None,
            blob_path=attached.blob_path if attached is not None else None,
            reset_progress=True,
        )
        refreshed = store.source(key, source_id)
        assert refreshed is not None
        blob = attached
    else:
        assert record is not None
        source_id = record.source_id
        alias = None
        if wire is not None and record.path != wire["path"]:
            alias = {
                "path": wire["path"],
                "device": wire["device"],
                "inode": wire["inode"],
                "size": wire["size"],
                "mtime_ns": wire["mtime_ns"],
            }
        identity = _identity(reference)
        unchanged = (
            alias is None
            and not record.absent
            and (record.device, record.inode, record.size, record.mtime_ns, record.allocated_bytes)
            == (identity.device, identity.inode, identity.size, identity.mtime_ns, identity.allocated_bytes)
            and (record.compression, record.profile, record.log_kind, record.is_active)
            == (identity.compression, identity.profile, identity.log_kind, identity.is_active)
            and record.path == identity.path
        )
        if unchanged:
            refreshed = record
        else:
            store.record_source(
                key,
                source_id,
                identity,
                aliases=[alias] if alias is not None else None,
                in_initial_manifest=blob is not None,
                capture_id=blob.capture_id if blob is not None else None,
                blob_path=blob.blob_path if blob is not None else None,
            )
            refreshed = store.source(key, source_id) or record

    complete_for_current = sources.prefix_complete_for_current(refreshed, wire)
    read_wire = _select_read_wire(refreshed, blob, wire, complete_for_current)
    raw_size = 0
    if read_wire is not None:
        raw_size = read_wire["length"] if "blob_path" in read_wire else read_wire["size"]
    return _FilePlan(
        source_id=refreshed.source_id,
        wire=wire,
        identity=refreshed_identity(refreshed),
        read_wire=read_wire,
        raw_size=raw_size,
        start_offset=refreshed.decoded_offset,
        line_number=refreshed.line_number or 1,
        fragment_index=refreshed.fragment_index or 0,
        active_prefix=(
            _read_wire_active(read_wire, refreshed)
            if read_wire is not None and "blob_path" in read_wire
            else bool(wire and wire["is_active"])
        ),
        in_initial_manifest=refreshed.in_initial_manifest,
        complete_for_current=complete_for_current,
        verify_raw_size=verify_raw_size,
        verify_raw_sha256=verify_raw_sha256,
    )


def refreshed_identity(record: SourceRecord) -> SourceIdentity:
    return SourceIdentity(
        path=record.path,
        device=record.device,
        inode=record.inode,
        size=record.size,
        mtime_ns=record.mtime_ns,
        allocated_bytes=record.allocated_bytes,
        compression=record.compression,
        profile=record.profile,
        log_kind=record.log_kind,
        is_active=record.is_active,
    )


def _match_record(
    by_inode: Mapping[Tuple[int, int], SourceRecord],
    by_path: Mapping[str, SourceRecord],
    by_id: Mapping[str, SourceRecord],
    wire: Dict[str, Any],
) -> Optional[SourceRecord]:
    """Select the durable record that may own *wire*.

    An inode match is only honoured for the same logical source: a different
    PBS task UPID that re-used the device+inode is never selected as that task's
    rename/append lineage.  The path map is consulted only for a compatible
    record, so a row already rewritten to the impostor's path is rejected too.
    """
    record = by_inode.get((wire["device"], wire["inode"]))
    if record is not None and not sources.identity_compatible(record, wire):
        record = None
    if record is None:
        candidate = by_path.get(wire["path"])
        if candidate is not None and sources.identity_compatible(candidate, wire):
            record = candidate
    if record is None:
        candidate = by_id.get(base_source_id(wire["profile"], wire["log_kind"], wire["path"]))
        if candidate is not None and sources.identity_compatible(candidate, wire):
            record = candidate
    return record


def _matching_identity_capsule(
    store: CheckpointStore,
    key: GuestKey,
    canonical: str,
    record: SourceRecord,
    provenance: sources.SourceProvenance,
) -> Optional[PruneIntent]:
    """The canonical intent capsule that proves this row's archived identity.

    Archive-generation proof and deletion-completion proof are deliberately
    separate: the capsule (a pending *or* done prune intent for the canonical
    path) records the original device/inode/size/mtime/digest, and it is
    accepted only when it lines up exactly with the row's own preserved,
    acknowledged complete-provenance size and digest.  A pending capsule still
    proves the archived identity — but never a completed deletion — so callers
    must decide whether the file may be tombstoned; a pending intent is never
    read as completion.  Nothing here mutates the intent.
    """
    for intent in store.prune_intents(key):
        if intent.state not in ("pending", "done") or intent.path != canonical:
            continue
        if (intent.device, intent.inode) != (record.device, record.inode):
            continue
        if intent.digest is None or intent.digest != record.digest:
            continue
        if provenance.raw_sha256 not in (None, record.digest):
            continue
        if provenance.complete_size != intent.size:
            continue
        return intent
    return None


def _recover_reassigned_sources(store: CheckpointStore, key: GuestKey, state: "_ImportState") -> None:
    """Repair PBS task rows this bug already rewrote to another task's path.

    Restores the canonical path/identity and the original frozen/ACK provenance
    (and clears the false coverage conflict) *only* from an exact canonical
    capsule.  A ``done`` capsule additionally proves the owned deletion, so the
    reclaimed file is re-tombstoned; a ``pending`` capsule proves only the
    archived identity, so the row is restored without asserting deletion and
    the intent state is left untouched.  Without a capsule the row is left
    intact and the guest stays blocked; the impostor file is always archived
    later under its own identity, never as this row's continuation.
    """
    for record in store.sources(key):
        if record.profile != "pbs" or record.log_kind != "task":
            continue
        provenance = sources.SourceProvenance.parse(record.provenance)
        canonical = provenance.canonical_path
        if not canonical or canonical == record.path:
            continue
        if sources.same_logical_source(record.profile, record.log_kind, canonical, record.path):
            continue
        # A different task's path was written over this canonical task row.
        if record.source_id != base_source_id(record.profile, record.log_kind, canonical):
            state.failed = True
            state.warnings.append(
                f"housekeeping import: source {record.source_id[:12]} impersonated by another task "
                f"({_TASK_REASSIGNED_CODE}); retained unresolved, deletion blocked"
            )
            continue
        if record.digest is None or not record.acknowledged or not provenance.complete:
            state.failed = True
            state.warnings.append(
                f"housekeeping import: source {record.source_id[:12]} reassigned without frozen coverage "
                f"({_TASK_REASSIGNED_CODE}); retained unresolved, deletion blocked"
            )
            continue
        intent = _matching_identity_capsule(store, key, canonical, record, provenance)
        if intent is None:
            state.failed = True
            state.warnings.append(
                f"housekeeping import: source {record.source_id[:12]} reassigned with no matching canonical "
                f"capsule ({_TASK_REASSIGNED_CODE}); retained unresolved, deletion blocked"
            )
            continue
        deleted = intent.state == "done"
        _restore_reassigned_source(store, key, record, provenance, intent, deleted=deleted)
        state.warnings.append(
            f"housekeeping import: source {record.source_id[:12]} restored to its canonical task "
            f"({_TASK_REASSIGNED_CODE}, reclaim {'proven' if deleted else 'pending'}); "
            "the current file is archived under its own identity"
        )


def _restore_reassigned_source(
    store: CheckpointStore,
    key: GuestKey,
    record: SourceRecord,
    provenance: sources.SourceProvenance,
    intent: PruneIntent,
    *,
    deleted: bool,
) -> None:
    """Return one reassigned row to its canonical frozen/ACK identity.

    ``deleted`` is the deletion-completion proof (a ``done`` capsule): only then
    is the reclaimed file re-tombstoned.  A pending capsule restores the archived
    identity without asserting, or implying, any deletion.
    """
    restored = provenance.with_verification(
        verified_raw_size=provenance.verified_raw_size, conflict=False
    ).as_dict()
    kept_aliases = [
        alias
        for alias in (record.aliases or [])
        if isinstance(alias, dict)
        and sources.same_logical_source(record.profile, record.log_kind, intent.path, str(alias.get("path", "")))
    ]
    identity = SourceIdentity(
        path=intent.path,
        device=intent.device,
        inode=intent.inode,
        size=intent.size,
        mtime_ns=intent.mtime_ns,
        # Physical allocation is not in the capsule. No current-file coverage
        # is produced here; a current probe refreshes this conservative value.
        allocated_bytes=0,
        compression=record.compression,
        profile=record.profile,
        log_kind=record.log_kind,
        is_active=False,
    )
    store.record_source(
        key,
        record.source_id,
        identity,
        provenance=restored,
        aliases=kept_aliases,
        in_initial_manifest=record.in_initial_manifest,
        capture_id=record.capture_id,
        blob_path=record.blob_path,
        digest=record.digest,
    )
    if deleted:
        store.mark_source_absent(key, record.source_id)


def _plan_sources(state: _ImportState, wires: Sequence[Dict[str, Any]]) -> List[_FilePlan]:
    store = state.store
    key = state.key
    _recover_reassigned_sources(store, key, state)
    intent = store.capture_intent(key)
    records = list(store.sources(key))
    by_inode: Dict[Tuple[int, int], SourceRecord] = {}
    by_path: Dict[str, SourceRecord] = {}
    record: Optional[SourceRecord]
    for record in records:
        by_inode.setdefault((record.device, record.inode), record)
        by_path.setdefault(record.path, record)
        for alias in record.aliases or []:
            if isinstance(alias, dict) and "device" in alias and "inode" in alias:
                by_inode.setdefault((alias["device"], alias["inode"]), record)
    blob_by_source: Dict[str, BlobState] = {}
    manifest_wire_by_source: Dict[str, Dict[str, Any]] = {}
    blob: Optional[BlobState]
    if intent is not None:
        for entry in intent.manifest:
            if isinstance(entry, dict) and "blob_path" in entry:
                sid = base_source_id(entry.get("profile", "npm"), entry.get("log_kind", "application"), entry.get("path", ""))
                manifest_wire_by_source[sid] = entry
        for blob in intent.expected:
            blob_by_source[blob.source_id] = blob

    manifest_inode: Dict[Tuple[int, int], str] = {}
    for sid, blob in blob_by_source.items():
        manifest_inode.setdefault((blob.device, blob.inode), sid)

    existing_ids = [record.source_id for record in records]
    by_id = {record.source_id: record for record in records}
    current_paths = {wire["path"] for wire in wires}
    plans: List[_FilePlan] = []
    consumed: Set[str] = set()
    for wire in wires:
        record = _match_record(by_inode, by_path, by_id, wire)
        base_id: Optional[str] = None
        if record is not None:
            base_id = record.source_id
        else:
            # A frozen-manifest inode match must belong to the same logical
            # source: a re-used inode never lends a different PBS task the
            # manifest blob (and the frozen-prefix ACK) of another task.
            base_id = manifest_inode.get((wire["device"], wire["inode"]))
            if base_id is not None:
                manifest_source = blob_by_source.get(base_id)
                if (
                    manifest_source is None
                    or base_id != base_source_id(wire["profile"], wire["log_kind"], manifest_source.source_path)
                    or not sources.same_logical_source(
                        wire["profile"], wire["log_kind"], manifest_source.source_path, wire["path"]
                    )
                ):
                    base_id = None
            if base_id is None:
                base_id = base_source_id(wire["profile"], wire["log_kind"], wire["path"])
        blob = blob_by_source.get(base_id)
        if blob is not None:
            captured = store.captured_blob(key, blob.capture_id, blob.blob_path)
            if captured is not None:
                blob = captured
        if blob is None and record is not None and record.capture_id and record.blob_path:
            blob = store.captured_blob(key, record.capture_id, record.blob_path)
        manifest_wire = manifest_wire_by_source.get(base_id)
        same_generation = record is not None and sources.same_generation(record, wire, current_paths)
        # A frozen manifest identity (rename or first capture) is always planned;
        # otherwise a live/current tail that is not part of the frozen initial
        # manifest is left to the live Alloy reader.
        frozen_identity = blob is not None and (blob.device, blob.inode) == (wire["device"], wire["inode"])
        if (
            not same_generation
            and not frozen_identity
            and (wire["is_active"] or sources.current_log_name(wire["profile"], wire["log_kind"], wire["path"]))
        ):
            # Past the initial capture only *closed* files become archive
            # inputs: a newly discovered or replaced live/current tail is
            # already owned by the live Alloy reader and is archived once
            # rotation renames it or it leaves the current-name set.
            continue
        plan = _build_plan(
            store,
            key,
            wire,
            record,
            blob,
            manifest_wire,
            existing_ids=existing_ids,
            base_id=base_id,
            current_paths=current_paths,
        )
        consumed.add(plan.source_id)
        if record is None and base_id not in by_id:
            plan.lineage_predecessor = sources.lineage_predecessor(records, wire)
        plans.append(plan)

    if intent is not None:
        for blob in intent.expected:
            if blob.source_id in consumed:
                continue
            record = store.source(key, blob.source_id)
            if record is not None and sources.is_prefix_complete(record):
                continue
            captured = store.captured_blob(key, blob.capture_id, blob.blob_path) or blob
            plans.append(
                _build_plan(
                    store,
                    key,
                    None,
                    record,
                    captured,
                    manifest_wire_by_source.get(blob.source_id),
                    existing_ids=existing_ids,
                    base_id=blob.source_id,
                    current_paths=current_paths,
                )
            )
    plans.sort(key=lambda plan: (not plan.in_initial_manifest, plan.identity.mtime_ns, plan.source_id))
    return plans


# --------------------------------------------------------------------------- #
# Source import
# --------------------------------------------------------------------------- #


def _prepare_live_input(executor: Executor, state: _ImportState, plan: _FilePlan) -> bool:
    """Prove saved raw prefixes before rename/append/resume coverage is reused."""
    if plan.wire is None or (plan.read_wire is not None and "blob_path" in plan.read_wire):
        return True
    if plan.read_wire is None and not plan.complete_for_current:
        return True  # Active tail remains owned by the live reader.
    if plan.complete_for_current and plan.verify_raw_sha256 is None:
        return True
    record = state.store.source(state.key, plan.source_id)
    assert record is not None
    try:
        prefix = hashlib.sha256()
        position = 0
        with RemoteLogReader(executor, state.lxc_id, _live_read_wire(plan.wire)) as raw:
            while True:
                chunk = raw.read(65536)
                if not chunk:
                    break
                if position < plan.verify_raw_size:
                    prefix.update(chunk[:plan.verify_raw_size - position])
                position += len(chunk)
            digest = raw.sha256
        provenance = sources.SourceProvenance.parse(record.provenance)
        if plan.verify_raw_sha256 is not None and (
            position < plan.verify_raw_size or prefix.hexdigest() != plan.verify_raw_sha256
        ):
            state.store.record_source(
                state.key, record.source_id, refreshed_identity(record),
                provenance=provenance.with_verification(
                    verified_raw_size=provenance.verified_raw_size, conflict=True
                ).as_dict(),
                in_initial_manifest=record.in_initial_manifest,
                capture_id=record.capture_id, blob_path=record.blob_path,
            )
            raise RemoteSourceError("acknowledged source prefix changed", code="conflict")
        state.store.record_source(
            state.key, record.source_id, refreshed_identity(record),
            provenance=provenance.with_verification(verified_raw_size=position, conflict=False).as_dict(),
            in_initial_manifest=record.in_initial_manifest,
            capture_id=record.capture_id, blob_path=record.blob_path,
        )
        state.store.set_source_digest(state.key, record.source_id, digest)
        return True
    except (HousekeepingStreamError, OSError) as exc:
        # Bounded source id + typed code: enough to find the failing row without
        # exposing any shell, credential or log content.
        code = _bounded(getattr(exc, "code", None) or type(exc).__name__)
        state.failed = True
        state.stopped = True
        state.warnings.append(
            f"housekeeping import: source {plan.source_id[:12]} prefix verification failed "
            f"({code}); retained without deletion"
        )
        return False


def _try_lineage_reuse(
    executor: Executor,
    state: _ImportState,
    plan: _FilePlan,
    *,
    pool: Optional[RemoteSnapshotPool] = None,
) -> bool:
    """Reuse a proved predecessor's acknowledged coverage for a compression successor.

    We decode the successor fully and require matching full decoded SHA-256 and
    decoded length.  This never deduplicates unrelated files and never pushes
    the same messages twice; the successor's own raw compressed digest is still
    fingerprinted for prune authorisation.
    """
    predecessor = plan.lineage_predecessor
    if predecessor is None or plan.read_wire is None:
        return False
    predecessor = state.store.source(state.key, predecessor.source_id)
    if predecessor is None or not sources.is_prefix_complete(predecessor):
        return False
    record = state.store.source(state.key, plan.source_id)
    if record is None or sources.is_prefix_complete(record):
        return False
    try:
        with RemoteLogReader(executor, state.lxc_id, plan.read_wire, pool=pool) as raw:
            with DecodedLogReader(raw, plan.read_wire["compression"]) as decoded:
                while decoded.read(1 << 16):
                    pass
                decoded_sha = decoded.sha256
                decoded_bytes = decoded.decoded_bytes
            raw_sha = raw.sha256 or None
    except (HousekeepingStreamError, OSError):
        return False
    if raw_sha is None or decoded_sha is None:
        return False
    provenance = sources.SourceProvenance.parse(predecessor.provenance)
    if decoded_sha != provenance.decoded_sha256 or decoded_bytes != provenance.decoded_bytes:
        return False
    _mark_complete(
        state,
        plan,
        raw_sha,
        decoded_sha,
        decoded_bytes,
        lineage=predecessor.source_id,
    )
    current = state.store.source(state.key, plan.source_id)
    if plan.wire is not None and current is not None:
        state.add_covered(plan.wire, current)
    return True


def _flush_values(
    state: _ImportState,
    source_id: str,
    labels: Dict[str, str],
    values: List[Tuple[int, bytes, int, int, int]],
    start_offset: int,
    *,
    completion: PrefixCompletion,
    digest: Optional[str] = None,
) -> bool:
    """Persist the exact body, push it, and advance progress only on HTTP 204.

    ``completion`` decides what a 204 means: the decoded offset/digest always
    advance, but only :data:`PrefixCompletion.COMPLETE` (the immutable frozen
    prefix is fully consumed) lets the durable row join the initial-manifest
    gate.  A streaming body therefore stays unacknowledged however many times it
    is retried.
    """
    last_ts, _last, end_offset, end_line, end_frag = values[-1]
    body = _build_body(_labels_json(labels), [value[1] for value in values])
    if len(body) > MAX_BODY_BYTES:
        state.failed = True
        state.warnings.append("housekeeping import: internal batch bound exceeded")
        return False
    batch_id = f"{source_id}.{end_offset}.{uuid.uuid4().hex[:12]}"
    progress = BatchProgress(
        source_id=source_id,
        decoded_offset=end_offset,
        line_number=end_line,
        fragment_index=end_frag,
        stream_ts_ns=last_ts,
        completion=completion,
        digest=digest,
    )
    state.store.save_pending_batch(
        state.key, batch_id, body, [progress.to_delta()], created_ns=state.clock_ns()
    )
    outcome = push_batch(state.push_url, body, sleep=state.sleep)
    if outcome.ok:
        state.store.acknowledge_batch(state.key, batch_id)
        state.bytes_archived += max(end_offset - start_offset, 0)
        state.body_bytes += len(body)
        state.observe_ts(last_ts)
        return True
    state.stopped = True
    state.failed = True
    if outcome.retryable:
        state.pending = True
        state.warnings.append(f"housekeeping import: loki push pending ({outcome.detail})")
    else:
        state.failed = True
        state.warnings.append(f"housekeeping import: loki push failed ({outcome.detail})")
    return False


def _import_source(
    executor: Executor,
    state: _ImportState,
    plan: _FilePlan,
    *,
    pool: Optional[RemoteSnapshotPool] = None,
) -> None:
    if plan.read_wire is None:
        return
    if plan.lineage_predecessor is not None and _try_lineage_reuse(executor, state, plan, pool=pool):
        return
    record = state.store.source(state.key, plan.source_id)
    if record is None:
        return
    labels = _archive_labels(state.key, state.name, plan.read_wire)
    labels_json = _labels_json(labels)
    empty_size = len(_build_body(labels_json, []))
    cursor = plan.start_offset
    values: List[Tuple[int, bytes, int, int, int]] = []
    body_size = empty_size
    completed = False
    raw_sha: Optional[str] = None
    decoded_sha: Optional[str] = None
    decoded_bytes = 0
    try:
        with RemoteLogReader(executor, state.lxc_id, plan.read_wire, pool=pool) as raw:
            with DecodedLogReader(raw, plan.read_wire["compression"]) as decoded:
                fragments = iter_log_fragments(
                    decoded,
                    filename=record.path,
                    source_id=plan.source_id,
                    start_offset=plan.start_offset,
                    line_number=plan.line_number,
                    fragment_index=plan.fragment_index,
                    active_prefix=plan.active_prefix,
                )
                for fragment in fragments:
                    timestamp = state.next_ts()
                    encoded = _encode_value(timestamp, encode_entry(fragment.entry))
                    addition = len(encoded) + (1 if values else 0)
                    if values and (
                        body_size + addition > min(MAX_BODY_BYTES, state.budget - state.body_bytes)
                        or state.bytes_archived + fragment.next_offset - cursor > state.budget
                    ):
                        if not _flush_values(
                            state, plan.source_id, labels, values, cursor,
                            completion=PrefixCompletion.INCOMPLETE,
                        ):
                            return
                        cursor = values[-1][2]
                        values = []
                        body_size = empty_size
                        addition = len(encoded)
                    if (
                        body_size + addition > min(MAX_BODY_BYTES, state.budget - state.body_bytes)
                        or state.bytes_archived + fragment.next_offset - cursor > state.budget
                    ):
                        state.stopped = True
                        break
                    values.append(
                        (
                            timestamp,
                            encoded,
                            fragment.next_offset,
                            fragment.next_line_number,
                            fragment.next_fragment_index,
                        )
                    )
                    body_size += addition
                    if state.budget_reached():
                        break
                completed = decoded.at_eof and not state.stopped and not state.budget_reached()
            raw_sha = raw.sha256 or None
            decoded_sha = decoded.sha256 or None
            decoded_bytes = decoded.decoded_bytes
    except RemoteSourceError as exc:
        # Renamed/replaced/truncated inputs are reported for re-probe; unacknowledged
        # bytes are untouched and the run is blocked (never silently completed).
        # The bounded source id + typed code make the failing row identifiable
        # without leaking any log content.
        state.failed = True
        state.warnings.append(
            "housekeeping import: source "
            f"{plan.source_id[:12]} conflict, re-probe needed "
            f"({_bounded(getattr(exc, 'code', 'error'))})"
        )
        return
    except (HousekeepingStreamError, OSError) as exc:
        state.failed = True
        state.warnings.append(f"housekeeping import: stream failure ({_bounded(type(exc).__name__)})")
        return

    final_digest = raw_sha if completed else None
    if values and not _flush_values(
        state, plan.source_id, labels, values, cursor,
        completion=PrefixCompletion.COMPLETE if completed else PrefixCompletion.INCOMPLETE,
        digest=final_digest,
    ):
        return

    if completed and raw_sha is not None:
        _mark_complete(state, plan, raw_sha, decoded_sha, decoded_bytes)
    elif state.budget_reached():
        state.stopped = True

    current = state.store.source(state.key, plan.source_id)
    if plan.wire is not None and current is not None:
        state.add_covered(plan.wire, current)


def _mark_complete(
    state: _ImportState,
    plan: _FilePlan,
    raw_sha: str,
    decoded_sha: Optional[str],
    decoded_bytes: int,
    *,
    lineage: Optional[str] = None,
) -> None:
    record = state.store.source(state.key, plan.source_id)
    if record is None:
        return
    frozen = plan.read_wire is not None and "blob_path" in plan.read_wire
    provenance = sources.SourceProvenance.parse(record.provenance).with_completion(
        size=plan.raw_size,
        raw_sha256=raw_sha,
        decoded_sha256=decoded_sha,
        decoded_bytes=decoded_bytes,
        frozen=frozen,
        lineage=lineage,
    )
    identity = _identity(plan.wire) if plan.wire is not None else refreshed_identity(record)
    state.store.record_source(
        state.key,
        plan.source_id,
        identity,
        provenance=provenance.as_dict(),
        in_initial_manifest=record.in_initial_manifest,
        capture_id=record.capture_id,
        blob_path=record.blob_path,
    )
    state.store.set_source_digest(state.key, plan.source_id, raw_sha)
    state.store.mark_source_acknowledged(state.key, plan.source_id)


# --------------------------------------------------------------------------- #
# Initial capture
# --------------------------------------------------------------------------- #


def _capture_files(wires: Sequence[Dict[str, Any]], capture_id: str) -> List[Dict[str, Any]]:
    out = []
    for wire in wires:
        source_id = base_source_id(wire["profile"], wire["log_kind"], wire["path"])
        entry = dict(wire)
        entry["blob_path"] = blob_path_for(capture_id, source_id)
        entry["capture_id"] = capture_id
        out.append(entry)
    return out


def _ensure_capture(
    executor: Executor, store: CheckpointStore, key: GuestKey, wires: Sequence[Dict[str, Any]], state: _ImportState
) -> bool:
    intent = store.capture_intent(key)
    if intent is not None and intent.state == "complete":
        return True
    if intent is None:
        capture_id = capture_id_for(key)
        manifest = _capture_files(wires, capture_id)
        expected: List[BlobState] = []
        for wire, entry in zip(wires, manifest):
            expected.append(
                BlobState(
                    capture_id=capture_id,
                    source_id=base_source_id(wire["profile"], wire["log_kind"], wire["path"]),
                    source_path=wire["path"],
                    blob_path=entry["blob_path"],
                    device=wire["device"],
                    inode=wire["inode"],
                    compression=wire["compression"],
                    high_water_size=wire["size"],
                )
            )
        try:
            store.begin_capture(key, capture_id, manifest, expected)
        except CheckpointError as exc:
            state.failed = True
            state.warnings.append(f"housekeeping import: capture intent conflict ({_bounded(str(exc))})")
            return False
        intent = store.capture_intent(key)
    assert intent is not None
    capture_id = intent.capture_id
    if not intent.manifest:
        store.finish_capture(key, capture_id, state="complete")
        return True
    try:
        result = executor.housekeeping_capture(
            key.lxc_id, files=[dict(entry) for entry in intent.manifest], capture_id=capture_id
        )
    except Exception as exc:  # noqa: BLE001 - transport boundary, fail closed
        state.failed = True
        state.warnings.append(f"housekeeping import: capture transport error ({_bounded(type(exc).__name__)})")
        return False
    if result.failed:
        state.failed = True
        state.warnings.append("housekeeping import: initial capture failed; history retained")
        return False
    facts = result.facts if isinstance(result.facts, dict) else {}
    captured = facts.get("files")
    if not isinstance(captured, list) or len(captured) != len(intent.expected):
        state.failed = True
        state.warnings.append("housekeeping import: capture returned an unexpected manifest")
        return False
    declared = {blob.blob_path for blob in intent.expected}
    seen: Set[str] = set()
    for entry in captured:
        if not isinstance(entry, dict):
            state.failed = True
            state.warnings.append("housekeeping import: capture entry malformed")
            return False
        blob_path = entry.get("blob_path")
        sha256 = entry.get("sha256")
        captured_bytes = entry.get("captured_bytes")
        if blob_path not in declared or blob_path in seen:
            state.failed = True
            state.warnings.append("housekeeping import: capture file not in the frozen manifest")
            return False
        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
            state.failed = True
            state.warnings.append("housekeeping import: capture digest invalid")
            return False
        if not isinstance(captured_bytes, int) or isinstance(captured_bytes, bool) or captured_bytes < 0:
            captured_bytes = 0
        store.mark_blob_captured(key, capture_id, blob_path, sha256=sha256, captured_bytes=captured_bytes)
        seen.add(blob_path)
    if seen != declared:
        state.failed = True
        state.warnings.append("housekeeping import: capture incomplete manifest")
        return False
    store.finish_capture(key, capture_id, state="complete")
    return True


# --------------------------------------------------------------------------- #
# Pending batch resume
# --------------------------------------------------------------------------- #


def _pending_decoded_bytes(store: CheckpointStore, key: GuestKey, pending: PendingBatch) -> int:
    total = 0
    for delta in pending.progress:
        record = store.source(key, delta.source_id)
        if record is not None:
            total += max(delta.decoded_offset - record.decoded_offset, 0)
    return total


def _flush_existing_pending(state: _ImportState, executor: Executor) -> Optional[ImportResult]:
    pending = state.store.pending_batch(state.key)
    if pending is None:
        return None
    state.observe_ts(max((delta.stream_ts_ns for delta in pending.progress), default=0))
    if state.clock_ns() - pending.created_ns > STALE_PENDING_NS:
        if not _rebuild_pending(state, executor, pending):
            if not state.failed:
                state.pending = True
            return ImportResult(
                bytes_archived=state.bytes_archived,
                pending=not state.failed,
                failed=state.failed,
                warnings=state.warnings,
                covered_files=state.covered,
            )
        pending = state.store.pending_batch(state.key)
        if pending is None:
            return None
    outcome = push_batch(state.push_url, pending.body, sleep=state.sleep)
    if outcome.ok:
        state.bytes_archived += _pending_decoded_bytes(state.store, state.key, pending)
        state.body_bytes += len(pending.body)
        state.store.acknowledge_batch(state.key, pending.batch_id)
        return None
    if outcome.retryable:
        state.pending = True
        state.warnings.append(f"housekeeping import: loki push pending ({outcome.detail})")
        return ImportResult(
            bytes_archived=state.bytes_archived,
            pending=True,
            failed=True,
            warnings=state.warnings,
            covered_files=state.covered,
        )
    state.failed = True
    state.warnings.append(f"housekeeping import: loki push failed ({outcome.detail})")
    return ImportResult(bytes_archived=state.bytes_archived, failed=True, warnings=state.warnings)


def _rebuild_pending(state: _ImportState, executor: Executor, pending: PendingBatch) -> bool:
    """Re-read a >24h batch's exact range and replace it with fresh timestamps."""
    if len(pending.progress) != 1:
        state.failed = True
        state.warnings.append("housekeeping import: stale multi-source batch cannot be re-timestamped safely")
        return False
    delta = pending.progress[0]
    record = state.store.source(state.key, delta.source_id)
    if record is None:
        state.failed = True
        state.warnings.append("housekeeping import: stale batch references an unknown source")
        return False
    blob = state.store.captured_blob(state.key, record.capture_id, record.blob_path) if record.capture_id and record.blob_path else None
    read_wire = _select_read_wire(record, blob, None, False) or _blob_or_live_wire(record, blob)
    if read_wire is None:
        state.failed = True
        state.warnings.append("housekeeping import: stale batch source is no longer readable")
        return False
    labels = _archive_labels(state.key, state.name, read_wire)
    values: List[Tuple[int, bytes, int, int, int]] = []
    try:
        with RemoteLogReader(executor, state.lxc_id, read_wire) as raw:
            with DecodedLogReader(raw, read_wire["compression"]) as decoded:
                regions = iter_log_fragments(
                    decoded,
                    filename=record.path,
                    source_id=record.source_id,
                    start_offset=record.decoded_offset,
                    line_number=record.line_number or 1,
                    fragment_index=record.fragment_index or 0,
                    active_prefix=_read_wire_active(read_wire, record),
                )
                for fragment in regions:
                    if fragment.next_offset > delta.decoded_offset:
                        state.failed = True
                        state.warnings.append("housekeeping import: stale batch no longer matches its source")
                        return False
                    timestamp = state.next_ts()
                    values.append(
                        (
                            timestamp,
                            _encode_value(timestamp, encode_entry(fragment.entry)),
                            fragment.next_offset,
                            fragment.next_line_number,
                            fragment.next_fragment_index,
                        )
                    )
                    if fragment.next_offset == delta.decoded_offset:
                        break
    except (HousekeepingStreamError, OSError) as exc:
        state.failed = True
        state.warnings.append(f"housekeeping import: stale batch rebuild failed ({_bounded(type(exc).__name__)})")
        return False
    if not values or values[-1][2] != delta.decoded_offset:
        state.failed = True
        state.warnings.append("housekeeping import: stale batch range could not be reproduced")
        return False
    body = _build_body(_labels_json(labels), [value[1] for value in values])
    progress = ProgressDelta(
        source_id=record.source_id,
        decoded_offset=values[-1][2],
        line_number=values[-1][3],
        fragment_index=values[-1][4],
        stream_ts_ns=values[-1][0],
        acknowledged=delta.acknowledged,
        digest=delta.digest,
    )
    state.store.replace_pending_batch(state.key, pending.batch_id, body, [progress], created_ns=state.clock_ns())
    return True


def _blob_or_live_wire(record: SourceRecord, blob: Optional[BlobState]) -> Optional[Dict[str, Any]]:
    if blob is not None and blob.captured:
        return _blob_read_wire(blob, record)
    wire = {
        "path": record.path,
        "device": record.device,
        "inode": record.inode,
        "size": record.size,
        "mtime_ns": record.mtime_ns,
        "allocated_bytes": record.allocated_bytes,
        "compression": record.compression,
        "profile": record.profile,
        "log_kind": record.log_kind,
        "is_active": record.is_active,
    }
    return _live_read_wire(wire)


# --------------------------------------------------------------------------- #
# Release
# --------------------------------------------------------------------------- #


def _maybe_release(executor: Executor, state: _ImportState) -> None:
    intent = state.store.capture_intent(state.key)
    if intent is None or intent.state != "complete":
        return
    if state.store.pending_batch(state.key) is not None:
        return
    blobs = state.store.captured_blobs(state.key, intent.capture_id)
    records = {record.source_id: record for record in state.store.sources(state.key)}
    candidates: List[str] = []
    for blob in blobs:
        if blob.released or not blob.captured:
            continue
        record = records.get(blob.source_id)
        if record is None or not sources.is_prefix_complete(record):
            continue
        provenance = sources.SourceProvenance.parse(record.provenance)
        if provenance.blob_sha256 == blob.sha256 or record.digest == blob.sha256:
            candidates.append(blob.blob_path)
    if not candidates:
        return
    for start in range(0, len(candidates), _MAX_RELEASE_BLOBS):
        batch = candidates[start:start + _MAX_RELEASE_BLOBS]
        command = build_capture_release_command(intent.capture_id, batch)
        try:
            result = executor.run_shell(command)
        except Exception as exc:  # noqa: BLE001 - transport boundary
            state.warnings.append(f"housekeeping import: spool release transport error ({_bounded(type(exc).__name__)})")
            return
        if result.failed:
            state.warnings.append("housekeeping import: spool release failed; acknowledged blobs retained")
            return
        for blob_path in batch:
            try:
                state.store.release_blob(state.key, intent.capture_id, blob_path)
            except CheckpointError:
                return


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #


def _audit(store: CheckpointStore, key: GuestKey, wires: Sequence[Dict[str, Any]], warnings: List[str]) -> ImportResult:
    records = list(store.sources(key))
    by_inode: Dict[Tuple[int, int], SourceRecord] = {}
    by_path: Dict[str, SourceRecord] = {}
    by_id = {record.source_id: record for record in records}
    record: Optional[SourceRecord]
    for record in records:
        by_inode.setdefault((record.device, record.inode), record)
        by_path.setdefault(record.path, record)
        for alias in record.aliases or []:
            if isinstance(alias, dict) and "device" in alias and "inode" in alias:
                by_inode.setdefault((alias["device"], alias["inode"]), record)
    covered: List[Dict[str, Any]] = []
    pending_findings = 0
    for wire in wires:
        record = _match_record(by_inode, by_path, by_id, wire)
        if record is None or not sources.prefix_complete_for_current(record, wire):
            pending_findings += 1
            continue
        coverage = sources.current_coverage(record, wire)
        if coverage is not None:
            covered.append(coverage.to_wire())
    pending = store.pending_batch(key) is not None
    if pending:
        warnings.append("housekeeping import: a pending archive batch awaits acknowledgement")
    if pending_findings:
        warnings.append(f"housekeeping import: {pending_findings} file(s) still need archive coverage")
    return ImportResult(
        bytes_archived=0,
        pending=pending or pending_findings > 0,
        failed=False,
        warnings=warnings,
        covered_files=covered,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def import_guest_logs(
    executor: Executor,
    settings: GlobalSettings,
    store: CheckpointStore,
    key: GuestKey,
    *,
    name: str,
    files: Sequence[Dict[str, Any]],
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock_ns: Callable[[], int] = time.time_ns,
) -> ImportResult:
    """Import every retained file's acknowledged content for one guest.

    ``covered_files`` is the *only* prune authorisation this module returns: each
    entry proves complete archive coverage of the source's current identity
    (acknowledged, raw digest, matching device/inode/size) and is neither
    active, a current NPM/PBS live log name, nor control/task-index state.
    Newly discovered live/current tails are left to the live Alloy reader until
    rotation or closure turns them into closed archive inputs.
    """
    warnings: List[str] = []
    wires = _normalize_files(files, warnings)

    if dry_run:
        return _audit(store, key, wires, warnings)

    try:
        base_url = settings.require_loki_url()
    except (ValueError, AttributeError) as exc:
        return ImportResult(pending=False, failed=True, warnings=[f"housekeeping import: {_bounded(str(exc))}"])
    push_url = base_url.rstrip("/") + LOKI_PUSH_PATH

    budget = int(settings.housekeeping_backfill_budget_mb) * 1024 * 1024
    state = _ImportState(
        key=key,
        lxc_id=key.lxc_id,
        name=name,
        store=store,
        push_url=push_url,
        budget=budget,
        clock_ns=clock_ns,
        sleep=sleep,
        warnings=warnings,
    )
    record: Optional[SourceRecord]
    for record in store.sources(key):
        state.observe_ts(record.stream_ts_ns)

    if not _ensure_capture(executor, store, key, wires, state):
        return ImportResult(
            bytes_archived=state.bytes_archived,
            pending=False,
            failed=True,
            warnings=warnings,
            covered_files=state.covered,
        )

    existing_pending = _flush_existing_pending(state, executor)
    if existing_pending is not None:
        return existing_pending

    plans = _plan_sources(state, wires)
    pool_wires = [
        plan.read_wire for plan in plans
        if plan.read_wire is not None and _pool_eligible(plan.read_wire)
    ]
    # One lazily-fetched pool serves every eligible frozen range in plan order.
    # The live current-prefix proof that gates current-file deletion still reads
    # through the strict singleton reader, so no deletion is ever credited from
    # prefetched bytes; each source advances only on its own HTTP 204 (the pool
    # never acknowledges anything).
    with _snapshot_pool(executor, state.lxc_id, pool_wires) as pool:
        for plan in plans:
            if state.stopped:
                break
            if not _prepare_live_input(executor, state, plan):
                break
            if plan.complete_for_current:
                if plan.wire is not None:
                    record = store.source(key, plan.source_id)
                    if record is not None:
                        state.add_covered(plan.wire, record)
                continue
            _import_source(executor, state, plan, pool=pool)

    if state.stopped and not state.failed:
        state.pending = True

    _maybe_release(executor, state)

    return ImportResult(
        bytes_archived=state.bytes_archived,
        pending=state.pending,
        failed=state.failed,
        warnings=warnings,
        covered_files=state.covered,
    )
