"""Durable source provenance and current-coverage invariants for log import.

This module is the single home for the *identity* slice of the acknowledged
log-housekeeping feature:

* :class:`SourceProvenance` -- the typed, tolerant view over a source's durable
  provenance JSON (frozen-prefix completion, generation, lineage);
* :class:`CurrentCoverage` -- complete archive coverage of a source's *current*
  identity, the only prune authorisation produced for a guest file;
* the pure predicates that decide whether two probes are the same logical
  generation, whether an immutable frozen prefix is complete, and which
  predecessor a compression successor may reuse.

It owns no transport, HTTP, checkpoint *mutation* or pruning policy: the
importer orchestrates upload/capture and the parent policy decides deletion.
Parsing keeps the exact persisted provenance keys (there is no second success
column) and stays tolerant of missing/foreign keys.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass, replace
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from proxmox_fleet.housekeeping_checkpoint import SourceRecord
from proxmox_fleet.housekeeping_io import NPM_ARCHIVE_RE, PBS_API_ARCHIVE_RE

__all__ = [
    "CurrentCoverage",
    "SourceProvenance",
    "current_coverage",
    "current_log_name",
    "identity_compatible",
    "is_prefix_complete",
    "lineage_predecessor",
    "logical_identity",
    "next_generation",
    "prefix_complete_for_current",
    "same_generation",
    "same_logical_source",
]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class SourceProvenance:
    """Typed view over a source's durable provenance JSON.

    The recorded keys are the existing provenance keys (there is no second
    success column): ``complete``/``complete_size`` are the immutable
    frozen-prefix completion, ``blob_sha256``/``blob_decoded_bytes`` record the
    frozen bytes that were acknowledged, and ``active_at_capture``/``generation``
    preserve logical identity across appends, renames and replacement
    generations.  Parsing is tolerant of missing/foreign keys.
    """

    canonical_path: str = ""
    profile: str = ""
    log_kind: str = ""
    active_at_capture: bool = False
    generation: int = 1
    complete: bool = False
    complete_size: int = 0
    capture_id: Optional[str] = None
    raw_sha256: Optional[str] = None
    decoded_sha256: Optional[str] = None
    decoded_bytes: int = 0
    blob_sha256: Optional[str] = None
    blob_decoded_bytes: Optional[int] = None
    lineage_of: Optional[str] = None
    verified_raw_size: int = 0
    coverage_conflict: bool = False

    @classmethod
    def parse(cls, raw: Optional[Mapping[str, Any]]) -> "SourceProvenance":
        if not isinstance(raw, Mapping):
            return cls()

        def _nonneg(name: str, default: int) -> int:
            value = raw.get(name, default)
            return value if _is_int(value) and value >= 0 else default

        def _text(name: str) -> Optional[str]:
            value = raw.get(name)
            return value if isinstance(value, str) and value else None

        return cls(
            canonical_path=str(raw.get("canonical_path") or ""),
            profile=str(raw.get("profile") or ""),
            log_kind=str(raw.get("log_kind") or ""),
            active_at_capture=raw.get("active_at_capture") is True,
            generation=max(_nonneg("generation", 1), 1),
            complete=raw.get("complete") is True,
            complete_size=_nonneg("complete_size", 0),
            capture_id=_text("capture_id"),
            raw_sha256=_text("raw_sha256"),
            decoded_sha256=_text("decoded_sha256"),
            decoded_bytes=_nonneg("decoded_bytes", 0),
            blob_sha256=_text("blob_sha256"),
            blob_decoded_bytes=(
                None if raw.get("blob_decoded_bytes") is None else _nonneg("blob_decoded_bytes", 0)
            ),
            lineage_of=_text("lineage_of"),
            verified_raw_size=_nonneg("verified_raw_size", 0),
            coverage_conflict=raw.get("coverage_conflict") is True,
        )

    @classmethod
    def fresh(cls, wire: Dict[str, Any], capture_id: Optional[str], *, generation: int = 1) -> "SourceProvenance":
        return cls(
            canonical_path=str(wire["path"]),
            profile=str(wire["profile"]),
            log_kind=str(wire["log_kind"]),
            active_at_capture=bool(wire["is_active"]),
            generation=max(int(generation), 1),
            capture_id=capture_id,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "canonical_path": self.canonical_path,
            "profile": self.profile,
            "log_kind": self.log_kind,
            "active_at_capture": self.active_at_capture,
            "generation": self.generation,
            "complete": self.complete,
            "complete_size": self.complete_size,
            "capture_id": self.capture_id,
            "raw_sha256": self.raw_sha256,
            "decoded_sha256": self.decoded_sha256,
            "decoded_bytes": self.decoded_bytes,
            "blob_sha256": self.blob_sha256,
            "blob_decoded_bytes": self.blob_decoded_bytes,
            "lineage_of": self.lineage_of,
            "verified_raw_size": self.verified_raw_size,
            "coverage_conflict": self.coverage_conflict,
        }

    def with_completion(
        self,
        *,
        size: int,
        raw_sha256: str,
        decoded_sha256: Optional[str],
        decoded_bytes: int,
        frozen: bool,
        lineage: Optional[str] = None,
    ) -> "SourceProvenance":
        """Record that the immutable frozen prefix is fully acknowledged."""
        return replace(
            self,
            complete=True,
            complete_size=max(int(size), 0),
            raw_sha256=raw_sha256,
            decoded_sha256=decoded_sha256,
            decoded_bytes=max(int(decoded_bytes), 0),
            blob_sha256=raw_sha256 if frozen else self.blob_sha256,
            blob_decoded_bytes=max(int(decoded_bytes), 0) if frozen else self.blob_decoded_bytes,
            lineage_of=lineage if lineage is not None else self.lineage_of,
        )

    def with_verification(self, *, verified_raw_size: int, conflict: bool) -> "SourceProvenance":
        return replace(
            self,
            verified_raw_size=max(int(verified_raw_size), 0),
            coverage_conflict=conflict,
        )


@dataclass(frozen=True)
class CurrentCoverage:
    """Complete acknowledged archive coverage of a source's *current* identity.

    This is the only prune authorisation produced for a guest file.  The wire
    keeps the current probe's exact identity (device/inode/size/mtime) so a
    consumer can cross-check it against the durable source record before
    deleting.
    """

    wire: Dict[str, Any]
    source_id: str
    sha256: str
    provenance_path: str

    def to_wire(self) -> Dict[str, Any]:
        entry = dict(self.wire)
        entry["sha256"] = self.sha256
        entry["source_id"] = self.source_id
        entry["provenance_path"] = self.provenance_path
        return entry


def current_log_name(profile: str, log_kind: str, path: str) -> bool:
    """True when *path* names a live/current log that native rotation owns.

    NPM application logs and the PBS access/auth logs are written in place and
    only become archive inputs once rotation renames them, so a current name is
    never prune-eligible even when no writer currently holds an fd.  The archive
    regexes are the same ones the retention policy uses.
    """
    name = posixpath.basename(path)
    if profile == "npm" and log_kind == "application":
        return NPM_ARCHIVE_RE.match(name) is None
    if profile == "pbs" and log_kind == "api":
        return PBS_API_ARCHIVE_RE.match(name) is None
    return False


def _plain_log_path(path: str) -> str:
    for suffix in (".gz", ".zst", ".zstd"):
        if path.endswith(suffix):
            return path[:-len(suffix)]
    return path


def logical_identity(profile: str, log_kind: str, path: str) -> Optional[str]:
    """The task identity embedded in a log's filename, when there is one.

    A PBS task log's filename *is* its UPID, which names exactly one task:
    re-using a device+inode for a *different* task is filesystem happenstance,
    never archive lineage.  The fan-out directory is derived from the UPID, so
    the basename stays stable across any legitimate rename of the same task.
    Every other kind has no filename-embedded identity and returns ``None``.
    """
    if profile != "pbs" or log_kind != "task":
        return None
    return posixpath.basename(_plain_log_path(path))


def same_logical_source(profile: str, log_kind: str, path_a: str, path_b: str) -> bool:
    """True unless *path_a* carries an identity that *path_b* does not share.

    Only the identity-bearing kinds can be unequal, so an NPM rotate or a plain
    append keeps its previous behaviour while a different PBS task UPID can
    never inherit another task's record.
    """
    identified = logical_identity(profile, log_kind, path_a)
    if identified is None:
        return True
    return identified == logical_identity(profile, log_kind, path_b)


def identity_compatible(record: SourceRecord, wire: Optional[Dict[str, Any]]) -> bool:
    """True when *wire* may belong to *record*'s logical source.

    Uses the record's *canonical* path from its durable provenance, so a record
    that an earlier build already rewrote to another task's path is still
    recognised as belonging to its original task and cannot be re-associated
    with the impostor by the re-used device+inode.
    """
    if wire is None:
        return True
    if record.profile != wire["profile"] or record.log_kind != wire["log_kind"]:
        return False
    canonical = SourceProvenance.parse(record.provenance).canonical_path or record.path
    return same_logical_source(record.profile, record.log_kind, canonical, str(wire["path"]))


def current_coverage(
    record: Optional[SourceRecord], wire: Optional[Dict[str, Any]]
) -> Optional[CurrentCoverage]:
    """Prune authorisation iff the *current* identity is fully archived.

    Returns ``None`` unless the durable source is acknowledged with a digest,
    the immutable frozen prefix is complete, the current device/inode and size
    still match the probe, and the file is neither active, a current live log
    name, nor PBS task-index/control state.  A frozen-prefix acknowledgement on
    top of a grown (or renamed) live file never authorises deletion.
    """
    if record is None or wire is None or record.digest is None or not record.acknowledged:
        return None
    if record.log_kind == "task_index" or record.is_active:
        return None
    if current_log_name(record.profile, record.log_kind, record.path):
        return None
    if not prefix_complete_for_current(record, wire):
        return None
    return CurrentCoverage(
        wire=wire,
        source_id=record.source_id,
        sha256=record.digest,
        provenance_path=record.path,
    )


def is_prefix_complete(record: Optional[SourceRecord]) -> bool:
    """True when *record* has an acknowledged digest over a complete prefix."""
    if record is None or record.digest is None:
        return False
    return SourceProvenance.parse(record.provenance).complete


def next_generation(existing_ids: Sequence[str], base: str) -> Tuple[str, int]:
    """Next ``<base>.gN`` source id and generation for a replaced identity."""
    highest = 1
    prefix = f"{base}.g"
    for source_id in existing_ids:
        if source_id.startswith(prefix) and source_id[len(prefix):].isdigit():
            highest = max(highest, int(source_id[len(prefix):]))
    generation = highest + 1
    return f"{base}.g{generation}", generation


def same_generation(
    record: SourceRecord, wire: Optional[Dict[str, Any]],
    current_paths: Mapping[str, Tuple[int, int]],
) -> bool:
    """True when *wire* is the same logical source (rename or append), not a reset."""
    if wire is None:
        return True
    if not identity_compatible(record, wire):
        return False
    if wire["compression"] != record.compression:
        return False
    if (record.device, record.inode) != (wire["device"], wire["inode"]):
        return False
    if record.size == wire["size"] and record.mtime_ns == wire["mtime_ns"]:
        return True
    # A recreated live pathname does not mean its former inode stayed there.
    renamed = (
        record.path != wire["path"]
        and current_paths.get(record.path) != (record.device, record.inode)
    )
    if wire["compression"] == "plain" and wire["size"] > record.size and (record.path == wire["path"] or renamed):
        return True
    return False


def prefix_complete_for_current(record: SourceRecord, wire: Optional[Dict[str, Any]]) -> bool:
    """True when the acknowledged immutable prefix equals the current file.

    Uses only the typed provenance view: the frozen-prefix completion
    (``complete``/``complete_size``) must match the current identity's size, so a
    frozen prefix on a grown live file is never treated as complete.
    """
    if wire is None or not identity_compatible(record, wire) or not is_prefix_complete(record):
        return False
    provenance = SourceProvenance.parse(record.provenance)
    if (record.device, record.inode) != (wire["device"], wire["inode"]):
        return False
    return provenance.complete_size == wire["size"]


def lineage_predecessor(records: Sequence[SourceRecord], wire: Dict[str, Any]) -> Optional[SourceRecord]:
    """A plain predecessor in this rotation family; coverage is checked at use."""
    if wire["compression"] not in ("gzip", "zstd"):
        return None
    path = wire["path"]
    predecessor_path = _plain_log_path(path)
    if predecessor_path == path:
        return None
    candidates = (
        record for record in records
        if record.path == predecessor_path
        and record.profile == wire["profile"]
        and record.log_kind == wire["log_kind"]
        and record.compression == "plain"
    )
    return max(
        candidates,
        key=lambda record: (
            SourceProvenance.parse(record.provenance).generation, record.updated_ns,
        ),
        default=None,
    )
