"""Typed SQLite checkpoint store for the recurring housekeeping feature.

The store is deliberately boring: it persists *data* — guest identity, source
provenance/progress, capture intent, the in-flight acknowledged-import HTTP
batch, prune intents and cache-clean cadence — and never makes policy
decisions.  The manager-side policy in ``proxmox_fleet.housekeeping`` owns age
cutoffs, budgets, cadence intervals and Loki readiness gating; this module owns
durability, atomicity and fail-closed behaviour.  There is no network I/O here.

Invariants
----------

* Schema version lives in ``PRAGMA user_version`` and must equal
  :data:`SCHEMA_VERSION`.  A mismatching or malformed database raises
  :class:`CheckpointSchemaError` / :class:`CheckpointCorruptError` — it is never
  reset, recreated or partially repaired.
* A pending push batch is persisted with its exact HTTP body bytes *before* the
  request is sent; :meth:`CheckpointStore.save_pending_batch` enforces the
  bounded body size.  Progress only advances through
  :meth:`CheckpointStore.acknowledge_batch`, which applies every progress delta
  and drops the pending batch in a single SQLite transaction (ack = HTTP 204).
* Dry-run opens an existing database read-only (``mode=ro`` plus
  ``PRAGMA query_only``) and creates nothing.  A missing database yields an
  empty read-only view whose reads return empties and whose writes raise
  :class:`CheckpointReadOnlyError`.
* Sources are keyed by ``(cluster, node, lxc_id)`` plus a stable ``source_id``,
  so identical node/LXC ids in different clusters never collide.
* Tombstones (sources whose files have disappeared) survive for
  :data:`ABSENT_TOMBSTONE_SECONDS` and are only ever removed by an explicit
  :meth:`CheckpointStore.purge_tombstones` call — never automatically.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

SCHEMA_VERSION = 1

#: Outer Loki push-body limit (plan: at most 512 KiB encoded body per request).
DEFAULT_PENDING_BODY_LIMIT = 512 * 1024

#: Tombstones are pruned only after their file has been absent this long.
ABSENT_TOMBSTONE_SECONDS = 30 * 24 * 60 * 60

#: Database filename inside ``fleet_history_dir``.
DB_FILENAME = "housekeeping.sqlite3"

CAPTURE_STATES: Tuple[str, ...] = ("pending", "complete", "failed")
PRUNE_STATES: Tuple[str, ...] = ("pending", "done", "restored", "conflict")

_COMPRESSIONS: Tuple[str, ...] = ("plain", "gzip", "zstd")
_PROFILES: Tuple[str, ...] = ("npm", "pbs")
_LOG_KINDS: Tuple[str, ...] = ("application", "task", "api", "task_index")

_REQUIRED_TABLES: Tuple[str, ...] = ("guest", "source", "blob", "pending_batch", "prune_intent", "cache_clean")

_SCHEMA_SQL: Tuple[str, ...] = (
    """
    CREATE TABLE guest (
        cluster TEXT NOT NULL,
        node TEXT NOT NULL,
        lxc_id TEXT NOT NULL,
        capture_id TEXT,
        capture_state TEXT,
        capture_manifest TEXT,
        capture_expected TEXT,
        capture_created_ns INTEGER,
        delivery_alloy_sha256 TEXT,
        delivery_policy_hashes TEXT,
        journal_marker_ns INTEGER,
        file_marker_ns INTEGER,
        delivery_verified_ns INTEGER,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY (cluster, node, lxc_id)
    )
    """,
    """
    CREATE TABLE source (
        cluster TEXT NOT NULL,
        node TEXT NOT NULL,
        lxc_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        path TEXT NOT NULL,
        device INTEGER NOT NULL,
        inode INTEGER NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        allocated_bytes INTEGER NOT NULL,
        compression TEXT NOT NULL,
        profile TEXT NOT NULL,
        log_kind TEXT NOT NULL,
        is_active INTEGER NOT NULL,
        provenance TEXT,
        aliases TEXT NOT NULL DEFAULT '[]',
        in_initial_manifest INTEGER NOT NULL DEFAULT 0,
        capture_id TEXT,
        blob_path TEXT,
        digest TEXT,
        decoded_offset INTEGER NOT NULL DEFAULT 0,
        line_number INTEGER NOT NULL DEFAULT 0,
        fragment_index INTEGER NOT NULL DEFAULT 0,
        stream_ts_ns INTEGER,
        acknowledged INTEGER NOT NULL DEFAULT 0,
        absent_since_ns INTEGER,
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY (cluster, node, lxc_id, source_id)
    )
    """,
    """
    CREATE TABLE blob (
        cluster TEXT NOT NULL,
        node TEXT NOT NULL,
        lxc_id TEXT NOT NULL,
        capture_id TEXT NOT NULL,
        blob_path TEXT NOT NULL,
        source_id TEXT NOT NULL,
        source_path TEXT NOT NULL,
        device INTEGER NOT NULL,
        inode INTEGER NOT NULL,
        compression TEXT NOT NULL,
        high_water_size INTEGER NOT NULL,
        sha256 TEXT,
        captured_bytes INTEGER,
        captured_ns INTEGER,
        released_ns INTEGER,
        PRIMARY KEY (cluster, node, lxc_id, capture_id, blob_path)
    )
    """,
    """
    CREATE TABLE pending_batch (
        cluster TEXT NOT NULL,
        node TEXT NOT NULL,
        lxc_id TEXT NOT NULL,
        batch_id TEXT NOT NULL,
        body BLOB NOT NULL,
        created_ns INTEGER NOT NULL,
        progress TEXT NOT NULL,
        PRIMARY KEY (cluster, node, lxc_id)
    )
    """,
    """
    CREATE TABLE prune_intent (
        intent_id TEXT NOT NULL,
        cluster TEXT NOT NULL,
        node TEXT NOT NULL,
        lxc_id TEXT NOT NULL,
        source_id TEXT,
        path TEXT NOT NULL,
        quarantine_path TEXT NOT NULL,
        device INTEGER NOT NULL,
        inode INTEGER NOT NULL,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        digest TEXT,
        state TEXT NOT NULL,
        detail TEXT,
        reclaimed_bytes INTEGER,
        created_ns INTEGER NOT NULL,
        resolved_ns INTEGER,
        PRIMARY KEY (intent_id)
    )
    """,
    """
    CREATE TABLE cache_clean (
        cluster TEXT NOT NULL,
        node TEXT NOT NULL,
        lxc_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        last_success_ns INTEGER NOT NULL,
        PRIMARY KEY (cluster, node, lxc_id, tool)
    )
    """,
    "CREATE INDEX idx_source_absent ON source (absent_since_ns)",
    "CREATE INDEX idx_prune_state ON prune_intent (state)",
)


def now_ns() -> int:
    """Current wall-clock time in nanoseconds since the epoch."""
    return time.time_ns()


def checkpoint_path(history_dir: Union[str, Path]) -> Path:
    """The checkpoint database path inside a fleet history directory."""
    return Path(history_dir) / DB_FILENAME


class CheckpointError(RuntimeError):
    """Base class for every checkpoint failure (fail closed, never reset)."""


class CheckpointSchemaError(CheckpointError):
    """The database declares an unknown schema version or is missing tables."""


class CheckpointCorruptError(CheckpointError):
    """SQLite rejected the file or a statement — treat the store as untrusted."""


class CheckpointReadOnlyError(CheckpointError):
    """A write was attempted on a read-only (dry-run) store."""


class CheckpointConflictError(CheckpointError):
    """Stored state contradicts the requested operation (stale id, unknown row)."""


# --------------------------------------------------------------------------- #
# Typed records
# --------------------------------------------------------------------------- #


def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _require_non_negative(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative int, got {value!r}")
    return value


@dataclass(frozen=True)
class GuestKey:
    """Cluster-qualified guest identity — the natural key of every table."""

    cluster: str
    node: str
    lxc_id: str

    def __post_init__(self) -> None:
        _require_text("cluster", self.cluster)
        _require_text("node", self.node)
        _require_text("lxc_id", self.lxc_id)

    @property
    def tuple(self) -> Tuple[str, str, str]:
        return (self.cluster, self.node, self.lxc_id)

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.cluster}/{self.node}/{self.lxc_id}"


@dataclass(frozen=True)
class SourceIdentity:
    """Raw filesystem identity from a probe (the wire file object)."""

    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    allocated_bytes: int
    compression: str
    profile: str
    log_kind: str
    is_active: bool

    def __post_init__(self) -> None:
        _require_text("path", self.path)
        for name in ("device", "inode", "size", "mtime_ns", "allocated_bytes"):
            _require_non_negative(name, getattr(self, name))
        if self.compression not in _COMPRESSIONS:
            raise ValueError(f"compression must be one of {_COMPRESSIONS}, got {self.compression!r}")
        if self.profile not in _PROFILES:
            raise ValueError(f"profile must be one of {_PROFILES}, got {self.profile!r}")
        if self.log_kind not in _LOG_KINDS:
            raise ValueError(f"log_kind must be one of {_LOG_KINDS}, got {self.log_kind!r}")
        if not isinstance(self.is_active, bool):
            raise ValueError(f"is_active must be a bool, got {self.is_active!r}")


@dataclass(frozen=True)
class SourceRecord:
    """Full persisted source row: identity + provenance + import progress."""

    key: GuestKey
    source_id: str
    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    allocated_bytes: int
    compression: str
    profile: str
    log_kind: str
    is_active: bool
    provenance: Optional[Dict[str, Any]]
    aliases: List[Dict[str, Any]]
    in_initial_manifest: bool
    capture_id: Optional[str]
    blob_path: Optional[str]
    digest: Optional[str]
    decoded_offset: int
    line_number: int
    fragment_index: int
    stream_ts_ns: Optional[int]
    acknowledged: bool
    absent_since_ns: Optional[int]
    updated_ns: int

    @property
    def absent(self) -> bool:
        return self.absent_since_ns is not None


@dataclass(frozen=True)
class ProgressDelta:
    """Import progress to commit for one source once its batch is acknowledged.

    ``digest`` optionally records the acknowledged source-prefix digest at the
    same time as the offset (prune authorization reads it back later).
    """

    source_id: str
    decoded_offset: int
    line_number: int
    fragment_index: int
    stream_ts_ns: int
    acknowledged: bool = True
    digest: Optional[str] = None

    def __post_init__(self) -> None:
        _require_text("source_id", self.source_id)
        for name in ("decoded_offset", "line_number", "fragment_index"):
            _require_non_negative(name, getattr(self, name))
        if not isinstance(self.stream_ts_ns, int) or isinstance(self.stream_ts_ns, bool) or self.stream_ts_ns <= 0:
            raise ValueError(f"stream_ts_ns must be a positive int, got {self.stream_ts_ns!r}")
        if not isinstance(self.acknowledged, bool):
            raise ValueError("acknowledged must be a bool")
        if self.digest is not None:
            _require_text("digest", self.digest)


@dataclass(frozen=True)
class PendingBatch:
    """The exact, bounded HTTP body awaiting an acknowledgement."""

    key: GuestKey
    batch_id: str
    body: bytes
    created_ns: int
    progress: List[ProgressDelta]


@dataclass(frozen=True)
class BlobState:
    """One expected or captured node-spool blob of a frozen initial manifest."""

    capture_id: str
    source_id: str
    source_path: str
    blob_path: str
    device: int
    inode: int
    compression: str
    high_water_size: int
    sha256: Optional[str] = None
    captured_bytes: Optional[int] = None
    captured_ns: Optional[int] = None
    released_ns: Optional[int] = None

    def __post_init__(self) -> None:
        _require_text("capture_id", self.capture_id)
        _require_text("source_id", self.source_id)
        _require_text("source_path", self.source_path)
        _require_text("blob_path", self.blob_path)
        for name in ("device", "inode", "high_water_size"):
            _require_non_negative(name, getattr(self, name))
        if self.compression not in _COMPRESSIONS:
            raise ValueError(f"compression must be one of {_COMPRESSIONS}, got {self.compression!r}")
        for name in ("captured_bytes", "captured_ns", "released_ns"):
            value = getattr(self, name)
            if value is not None:
                _require_non_negative(name, value)
        if (self.sha256 is None) != (self.captured_ns is None):
            raise ValueError("sha256 and captured_ns must be set together")

    @property
    def captured(self) -> bool:
        return self.sha256 is not None

    @property
    def released(self) -> bool:
        return self.released_ns is not None


@dataclass(frozen=True)
class CaptureIntent:
    """The frozen initial manifest committed before capture starts."""

    capture_id: str
    state: str
    manifest: List[Dict[str, Any]]
    expected: List[BlobState]
    created_ns: int


@dataclass(frozen=True)
class DeliveryVerification:
    """Marker query + policy-hash verification for one guest.

    The policy never re-runs markers while ``effective_alloy_sha256`` and
    ``policy_hashes`` still match what it observes.
    """

    effective_alloy_sha256: Optional[str]
    policy_hashes: Dict[str, str]
    journal_marker_ns: Optional[int]
    file_marker_ns: Optional[int]
    verified_ns: Optional[int]


@dataclass(frozen=True)
class GuestRecord:
    """Guest-level row: capture epoch plus delivery verification."""

    key: GuestKey
    capture_id: Optional[str]
    capture_state: Optional[str]
    delivery: Optional[DeliveryVerification]
    updated_ns: int


@dataclass(frozen=True)
class PruneIntent:
    """Recoverable record of an intended quarantine/delete of one closed file."""

    intent_id: str
    key: GuestKey
    path: str
    quarantine_path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    source_id: Optional[str] = None
    digest: Optional[str] = None
    state: str = "pending"
    detail: Optional[str] = None
    reclaimed_bytes: Optional[int] = None
    created_ns: int = 0
    resolved_ns: Optional[int] = None

    def __post_init__(self) -> None:
        _require_text("intent_id", self.intent_id)
        _require_text("path", self.path)
        _require_text("quarantine_path", self.quarantine_path)
        for name in ("device", "inode", "size", "mtime_ns"):
            _require_non_negative(name, getattr(self, name))
        if self.state not in PRUNE_STATES:
            raise ValueError(f"state must be one of {PRUNE_STATES}, got {self.state!r}")
        if self.reclaimed_bytes is not None:
            _require_non_negative("reclaimed_bytes", self.reclaimed_bytes)


@dataclass(frozen=True)
class InitialManifestState:
    """Derived progress of the frozen initial manifest (policy gate input)."""

    capture_id: Optional[str]
    capture_state: Optional[str]
    total: int
    acknowledged: int
    remaining: int
    pending_batch: bool


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #


def _delta_to_json(delta: ProgressDelta) -> Dict[str, Any]:
    return {
        "source_id": delta.source_id,
        "decoded_offset": delta.decoded_offset,
        "line_number": delta.line_number,
        "fragment_index": delta.fragment_index,
        "stream_ts_ns": delta.stream_ts_ns,
        "acknowledged": delta.acknowledged,
        "digest": delta.digest,
    }


def _delta_from_json(raw: Any) -> ProgressDelta:
    if not isinstance(raw, dict):
        raise CheckpointCorruptError(f"progress delta is not an object: {raw!r}")
    try:
        return ProgressDelta(
            source_id=raw["source_id"],
            decoded_offset=raw["decoded_offset"],
            line_number=raw["line_number"],
            fragment_index=raw["fragment_index"],
            stream_ts_ns=raw["stream_ts_ns"],
            acknowledged=raw.get("acknowledged", True),
            digest=raw.get("digest"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointCorruptError(f"invalid progress delta {raw!r}: {exc}") from exc


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _loads_json(value: Optional[str], what: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise CheckpointCorruptError(f"invalid JSON in {what}: {exc}") from exc


def _blob_from_row(row: sqlite3.Row) -> BlobState:
    return BlobState(
        capture_id=row["capture_id"],
        source_id=row["source_id"],
        source_path=row["source_path"],
        blob_path=row["blob_path"],
        device=row["device"],
        inode=row["inode"],
        compression=row["compression"],
        high_water_size=row["high_water_size"],
        sha256=row["sha256"],
        captured_bytes=row["captured_bytes"],
        captured_ns=row["captured_ns"],
        released_ns=row["released_ns"],
    )


def _source_from_row(row: sqlite3.Row) -> SourceRecord:
    aliases = _loads_json(row["aliases"], "source.aliases") or []
    if not isinstance(aliases, list):
        raise CheckpointCorruptError("source.aliases is not a list")
    provenance = _loads_json(row["provenance"], "source.provenance")
    if provenance is not None and not isinstance(provenance, dict):
        raise CheckpointCorruptError("source.provenance is not an object")
    return SourceRecord(
        key=GuestKey(row["cluster"], row["node"], row["lxc_id"]),
        source_id=row["source_id"],
        path=row["path"],
        device=row["device"],
        inode=row["inode"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        allocated_bytes=row["allocated_bytes"],
        compression=row["compression"],
        profile=row["profile"],
        log_kind=row["log_kind"],
        is_active=bool(row["is_active"]),
        provenance=provenance,
        aliases=aliases,
        in_initial_manifest=bool(row["in_initial_manifest"]),
        capture_id=row["capture_id"],
        blob_path=row["blob_path"],
        digest=row["digest"],
        decoded_offset=row["decoded_offset"],
        line_number=row["line_number"],
        fragment_index=row["fragment_index"],
        stream_ts_ns=row["stream_ts_ns"],
        acknowledged=bool(row["acknowledged"]),
        absent_since_ns=row["absent_since_ns"],
        updated_ns=row["updated_ns"],
    )


def _intent_from_row(row: sqlite3.Row) -> PruneIntent:
    return PruneIntent(
        intent_id=row["intent_id"],
        key=GuestKey(row["cluster"], row["node"], row["lxc_id"]),
        path=row["path"],
        quarantine_path=row["quarantine_path"],
        device=row["device"],
        inode=row["inode"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        source_id=row["source_id"],
        digest=row["digest"],
        state=row["state"],
        detail=row["detail"],
        reclaimed_bytes=row["reclaimed_bytes"],
        created_ns=row["created_ns"],
        resolved_ns=row["resolved_ns"],
    )


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class CheckpointStore:
    """Read/write handle to ``<fleet_history_dir>/housekeeping.sqlite3``.

    Construct with :meth:`open` (or the plain constructor).  Read-only handles
    tolerate a missing database and never create files; writable handles create
    and initialise the schema exactly once.
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        read_only: bool = False,
        pending_body_limit: int = DEFAULT_PENDING_BODY_LIMIT,
    ) -> None:
        if pending_body_limit <= 0:
            raise ValueError("pending_body_limit must be > 0")
        self._path = Path(path)
        self._read_only = bool(read_only)
        self._pending_body_limit = int(pending_body_limit)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._schema_version = 0
        self._open()

    # -- factory / lifecycle ------------------------------------------------ #

    @classmethod
    def open(
        cls,
        path: Union[str, Path],
        *,
        read_only: bool = False,
        pending_body_limit: int = DEFAULT_PENDING_BODY_LIMIT,
    ) -> "CheckpointStore":
        return cls(path, read_only=read_only, pending_body_limit=pending_body_limit)

    @classmethod
    def for_history_dir(
        cls,
        history_dir: Union[str, Path],
        *,
        read_only: bool = False,
        pending_body_limit: int = DEFAULT_PENDING_BODY_LIMIT,
    ) -> "CheckpointStore":
        return cls(
            checkpoint_path(history_dir),
            read_only=read_only,
            pending_body_limit=pending_body_limit,
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "CheckpointStore":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- introspection ------------------------------------------------------ #

    @property
    def path(self) -> Path:
        return self._path

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def schema_version(self) -> int:
        return self._schema_version

    @property
    def is_empty(self) -> bool:
        """True for an empty read-only view (database absent, nothing created)."""
        return self._conn is None

    @property
    def pending_body_limit(self) -> int:
        return self._pending_body_limit

    # -- open/initialise ---------------------------------------------------- #

    def _open(self) -> None:
        exists = self._path.exists()
        if self._read_only:
            if not exists:
                # Empty view: reads are empty, writes raise.  Nothing created.
                return
            conn = self._connect("ro")
            version = self._user_version(conn)
            tables = self._table_names(conn)
            if tables:
                self._assert_schema(version, tables)
            elif version != 0:
                raise CheckpointSchemaError(
                    f"{self._path}: user_version={version} but no tables found (refusing to reset)"
                )
            self._conn = conn
            self._schema_version = version
            self._execute(conn, "PRAGMA query_only = ON")
            return

        if not exists and not self._path.parent.exists():
            raise CheckpointError(f"cannot create {self._path}: parent directory does not exist")
        conn = self._connect("rwc")
        try:
            version = self._user_version(conn)
            tables = self._table_names(conn)
            if not tables:
                if version != 0:
                    raise CheckpointSchemaError(
                        f"{self._path}: user_version={version} but no tables found (refusing to reset)"
                    )
                self._create_schema(conn)
                version = SCHEMA_VERSION
            else:
                self._assert_schema(version, tables)
            self._execute(conn, "PRAGMA journal_mode = DELETE")
            self._execute(conn, "PRAGMA synchronous = FULL")
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        self._schema_version = version

    def _assert_schema(self, version: int, tables: Sequence[str]) -> None:
        if version != SCHEMA_VERSION:
            raise CheckpointSchemaError(
                f"{self._path}: unsupported schema user_version={version} "
                f"(expected {SCHEMA_VERSION}); refusing to modify it"
            )
        missing = [name for name in _REQUIRED_TABLES if name not in set(tables)]
        if missing:
            raise CheckpointSchemaError(f"{self._path}: missing required tables: {', '.join(missing)}")

    def _connect(self, mode: str) -> sqlite3.Connection:
        uri = f"{self._path.resolve().as_uri()}?mode={mode}"
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=30.0)
        except sqlite3.Error as exc:
            raise CheckpointError(f"cannot open checkpoint database {self._path}: {exc}") from exc
        conn.row_factory = sqlite3.Row
        return conn

    def _user_version(self, conn: sqlite3.Connection) -> int:
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise CheckpointCorruptError(f"{self._path} is not a usable SQLite database: {exc}") from exc

    def _table_names(self, conn: sqlite3.Connection) -> List[str]:
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        except sqlite3.DatabaseError as exc:
            raise CheckpointCorruptError(f"{self._path} is not a usable SQLite database: {exc}") from exc
        return [str(row[0]) for row in rows]

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        self._execute(conn, "BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA_SQL:
                self._execute(conn, statement)
            self._execute(conn, f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            with contextlib.suppress(sqlite3.DatabaseError):
                conn.execute("ROLLBACK")
            raise
        self._execute(conn, "COMMIT")

    # -- low-level execution ------------------------------------------------ #

    def _execute(
        self,
        conn: sqlite3.Connection,
        sql: str,
        params: Union[Sequence[Any], Dict[str, Any]] = (),
    ) -> sqlite3.Cursor:
        try:
            return conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise CheckpointConflictError(str(exc)) from exc
        except sqlite3.DatabaseError as exc:
            raise CheckpointCorruptError(f"{self._path}: {exc}") from exc
        except sqlite3.Error as exc:  # e.g. malformed parameter binding
            raise CheckpointError(f"{self._path}: {exc}") from exc

    def _read(self, sql: str, params: Union[Sequence[Any], Dict[str, Any]] = ()) -> List[sqlite3.Row]:
        conn = self._conn
        if conn is None:
            return []
        with self._lock:
            return list(self._execute(conn, sql, params))

    def _write(self, sql: str, params: Union[Sequence[Any], Dict[str, Any]] = ()) -> int:
        conn = self._require_writable()
        with self._lock:
            return int(self._execute(conn, sql, params).rowcount)

    def _require_writable(self) -> sqlite3.Connection:
        if self._read_only:
            raise CheckpointReadOnlyError(f"{self._path} is open read-only")
        conn = self._conn
        if conn is None:  # pragma: no cover - writable open always connects
            raise CheckpointError(f"{self._path} is not open")
        return conn

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._require_writable()
        with self._lock:
            self._execute(conn, "BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                with contextlib.suppress(sqlite3.DatabaseError):
                    conn.execute("ROLLBACK")
                raise
            self._execute(conn, "COMMIT")

    def _ensure_guest(self, conn: sqlite3.Connection, key: GuestKey, ts_ns: int) -> None:
        self._execute(
            conn,
            """
            INSERT INTO guest (cluster, node, lxc_id, updated_ns) VALUES (?, ?, ?, ?)
            ON CONFLICT (cluster, node, lxc_id) DO UPDATE SET updated_ns = excluded.updated_ns
            """,
            (key.cluster, key.node, key.lxc_id, ts_ns),
        )

    # -- guests ------------------------------------------------------------- #

    def guests(self) -> List[GuestKey]:
        rows = self._read("SELECT cluster, node, lxc_id FROM guest ORDER BY cluster, node, lxc_id")
        return [GuestKey(row["cluster"], row["node"], row["lxc_id"]) for row in rows]

    def guest_record(self, key: GuestKey) -> Optional[GuestRecord]:
        rows = self._read(
            "SELECT * FROM guest WHERE cluster = ? AND node = ? AND lxc_id = ?", key.tuple
        )
        if not rows:
            return None
        row = rows[0]
        delivery = self._delivery_from_row(row)
        return GuestRecord(
            key=key,
            capture_id=row["capture_id"],
            capture_state=row["capture_state"],
            delivery=delivery,
            updated_ns=row["updated_ns"],
        )

    def delete_guest(self, key: GuestKey) -> None:
        """Remove every row for one guest (explicit reset, never automatic)."""
        with self._transaction() as conn:
            for statement in (
                "DELETE FROM source WHERE cluster = ? AND node = ? AND lxc_id = ?",
                "DELETE FROM blob WHERE cluster = ? AND node = ? AND lxc_id = ?",
                "DELETE FROM pending_batch WHERE cluster = ? AND node = ? AND lxc_id = ?",
                "DELETE FROM cache_clean WHERE cluster = ? AND node = ? AND lxc_id = ?",
                "DELETE FROM prune_intent WHERE cluster = ? AND node = ? AND lxc_id = ?",
                "DELETE FROM guest WHERE cluster = ? AND node = ? AND lxc_id = ?",
            ):
                self._execute(conn, statement, key.tuple)

    # -- sources ------------------------------------------------------------ #

    def record_source(
        self,
        key: GuestKey,
        source_id: str,
        identity: SourceIdentity,
        *,
        provenance: Optional[Dict[str, Any]] = None,
        aliases: Optional[List[Dict[str, Any]]] = None,
        in_initial_manifest: bool = False,
        capture_id: Optional[str] = None,
        blob_path: Optional[str] = None,
        digest: Optional[str] = None,
        reset_progress: bool = False,
        ts_ns: Optional[int] = None,
    ) -> SourceRecord:
        """Insert or refresh one source's identity/metadata.

        Import progress is preserved across refreshes unless ``reset_progress``
        is set (a replaced/truncated inode is a new archive input).  A refreshed
        source is no longer considered absent.
        """
        _require_text("source_id", source_id)
        ts = now_ns() if ts_ns is None else ts_ns
        with self._transaction() as conn:
            self._ensure_guest(conn, key, ts)
            self._execute(
                conn,
                """
                INSERT INTO source (
                    cluster, node, lxc_id, source_id, path, device, inode, size, mtime_ns,
                    allocated_bytes, compression, profile, log_kind, is_active, provenance,
                    aliases, in_initial_manifest, capture_id, blob_path, digest,
                    decoded_offset, line_number, fragment_index, stream_ts_ns, acknowledged,
                    absent_since_ns, updated_ns
                ) VALUES (
                    :cluster, :node, :lxc_id, :source_id, :path, :device, :inode, :size, :mtime_ns,
                    :allocated_bytes, :compression, :profile, :log_kind, :is_active, :provenance,
                    :aliases_new, :in_initial_manifest, :capture_id, :blob_path, :digest,
                    0, 0, 0, NULL, 0, NULL, :ts_ns
                )
                ON CONFLICT (cluster, node, lxc_id, source_id) DO UPDATE SET
                    path = excluded.path,
                    device = excluded.device,
                    inode = excluded.inode,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    allocated_bytes = excluded.allocated_bytes,
                    compression = excluded.compression,
                    profile = excluded.profile,
                    log_kind = excluded.log_kind,
                    is_active = excluded.is_active,
                    provenance = COALESCE(excluded.provenance, source.provenance),
                    aliases = COALESCE(:aliases_upd, source.aliases),
                    in_initial_manifest = MAX(source.in_initial_manifest, excluded.in_initial_manifest),
                    capture_id = COALESCE(excluded.capture_id, source.capture_id),
                    blob_path = COALESCE(excluded.blob_path, source.blob_path),
                    digest = COALESCE(excluded.digest, source.digest),
                    decoded_offset = CASE WHEN :reset THEN 0 ELSE source.decoded_offset END,
                    line_number = CASE WHEN :reset THEN 0 ELSE source.line_number END,
                    fragment_index = CASE WHEN :reset THEN 0 ELSE source.fragment_index END,
                    stream_ts_ns = CASE WHEN :reset THEN NULL ELSE source.stream_ts_ns END,
                    acknowledged = CASE WHEN :reset THEN 0 ELSE source.acknowledged END,
                    absent_since_ns = NULL,
                    updated_ns = excluded.updated_ns
                """,
                {
                    "cluster": key.cluster,
                    "node": key.node,
                    "lxc_id": key.lxc_id,
                    "source_id": source_id,
                    "path": identity.path,
                    "device": identity.device,
                    "inode": identity.inode,
                    "size": identity.size,
                    "mtime_ns": identity.mtime_ns,
                    "allocated_bytes": identity.allocated_bytes,
                    "compression": identity.compression,
                    "profile": identity.profile,
                    "log_kind": identity.log_kind,
                    "is_active": 1 if identity.is_active else 0,
                    "provenance": None if provenance is None else _dumps(provenance),
                    "aliases_new": "[]" if aliases is None else _dumps(aliases),
                    "aliases_upd": None if aliases is None else _dumps(aliases),
                    "in_initial_manifest": 1 if in_initial_manifest else 0,
                    "capture_id": capture_id,
                    "blob_path": blob_path,
                    "digest": digest,
                    "reset": 1 if reset_progress else 0,
                    "ts_ns": ts,
                },
            )
        record = self.source(key, source_id)
        if record is None:  # pragma: no cover - insert above guarantees a row
            raise CheckpointError(f"source {source_id} vanished after write")
        return record

    def source(self, key: GuestKey, source_id: str) -> Optional[SourceRecord]:
        rows = self._read(
            "SELECT * FROM source WHERE cluster = ? AND node = ? AND lxc_id = ? AND source_id = ?",
            (key.cluster, key.node, key.lxc_id, source_id),
        )
        return _source_from_row(rows[0]) if rows else None

    def sources(self, key: GuestKey) -> List[SourceRecord]:
        rows = self._read(
            "SELECT * FROM source WHERE cluster = ? AND node = ? AND lxc_id = ? ORDER BY source_id",
            key.tuple,
        )
        return [_source_from_row(row) for row in rows]

    def add_source_alias(
        self,
        key: GuestKey,
        source_id: str,
        alias: Dict[str, Any],
        *,
        ts_ns: Optional[int] = None,
    ) -> bool:
        """Record a path/inode alias for a source; returns True when new."""
        if not isinstance(alias, dict):
            raise ValueError("alias must be a dict")
        ts = now_ns() if ts_ns is None else ts_ns
        with self._transaction() as conn:
            row = self._source_row(conn, key, source_id)
            aliases = _loads_json(row["aliases"], "source.aliases") or []
            if not isinstance(aliases, list):
                raise CheckpointCorruptError("source.aliases is not a list")
            marker = (alias.get("path"), alias.get("device"), alias.get("inode"))
            for existing in aliases:
                if isinstance(existing, dict) and (
                    existing.get("path"),
                    existing.get("device"),
                    existing.get("inode"),
                ) == marker:
                    return False
            aliases.append(alias)
            self._execute(
                conn,
                "UPDATE source SET aliases = ?, updated_ns = ? WHERE cluster = ? AND node = ? AND lxc_id = ? AND source_id = ?",
                (_dumps(aliases), ts, key.cluster, key.node, key.lxc_id, source_id),
            )
            return True

    def set_source_digest(
        self,
        key: GuestKey,
        source_id: str,
        digest: str,
        *,
        ts_ns: Optional[int] = None,
    ) -> None:
        _require_text("digest", digest)
        ts = now_ns() if ts_ns is None else ts_ns
        with self._transaction() as conn:
            self._source_row(conn, key, source_id)
            self._execute(
                conn,
                "UPDATE source SET digest = ?, updated_ns = ? WHERE cluster = ? AND node = ? AND lxc_id = ? AND source_id = ?",
                (digest, ts, key.cluster, key.node, key.lxc_id, source_id),
            )

    def set_source_capture(
        self,
        key: GuestKey,
        source_id: str,
        *,
        capture_id: str,
        blob_path: str,
        ts_ns: Optional[int] = None,
    ) -> None:
        _require_text("capture_id", capture_id)
        _require_text("blob_path", blob_path)
        ts = now_ns() if ts_ns is None else ts_ns
        with self._transaction() as conn:
            self._source_row(conn, key, source_id)
            self._execute(
                conn,
                """
                UPDATE source SET capture_id = ?, blob_path = ?, updated_ns = ?
                WHERE cluster = ? AND node = ? AND lxc_id = ? AND source_id = ?
                """,
                (capture_id, blob_path, ts, key.cluster, key.node, key.lxc_id, source_id),
            )

    def mark_source_acknowledged(
        self,
        key: GuestKey,
        source_id: str,
        *,
        ts_ns: Optional[int] = None,
    ) -> None:
        """Mark a fully-consumed source acknowledged without an HTTP batch.

        A zero-fragment completion (an empty file, or a source already at EOF
        with no new bytes) has no push body, so ``acknowledge_batch`` can never
        run.  This lets the initial-manifest gate advance without fabricating a
        delivery.  The source must already exist.
        """
        _require_text("source_id", source_id)
        ts = now_ns() if ts_ns is None else ts_ns
        with self._transaction() as conn:
            self._source_row(conn, key, source_id)
            self._execute(
                conn,
                "UPDATE source SET acknowledged = 1, updated_ns = ? "
                "WHERE cluster = ? AND node = ? AND lxc_id = ? AND source_id = ?",
                (ts, key.cluster, key.node, key.lxc_id, source_id),
            )

    def _source_row(self, conn: sqlite3.Connection, key: GuestKey, source_id: str) -> sqlite3.Row:
        rows = list(
            self._execute(
                conn,
                "SELECT * FROM source WHERE cluster = ? AND node = ? AND lxc_id = ? AND source_id = ?",
                (key.cluster, key.node, key.lxc_id, source_id),
            )
        )
        if not rows:
            raise CheckpointConflictError(f"unknown source {source_id!r} for guest {key}")
        return rows[0]

    # -- initial manifest progress ------------------------------------------ #

    def initial_manifest_state(self, key: GuestKey) -> InitialManifestState:
        rows = self._read(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(acknowledged), 0) AS acknowledged
            FROM source WHERE cluster = ? AND node = ? AND lxc_id = ? AND in_initial_manifest = 1
            """,
            key.tuple,
        )
        total = int(rows[0]["total"]) if rows else 0
        acknowledged = int(rows[0]["acknowledged"]) if rows else 0
        guest = self.guest_record(key)
        pending = self.pending_batch(key) is not None
        return InitialManifestState(
            capture_id=None if guest is None else guest.capture_id,
            capture_state=None if guest is None else guest.capture_state,
            total=total,
            acknowledged=acknowledged,
            remaining=max(total - acknowledged, 0),
            pending_batch=pending,
        )

    # -- pending HTTP batch -------------------------------------------------- #

    def save_pending_batch(
        self,
        key: GuestKey,
        batch_id: str,
        body: bytes,
        progress: Sequence[ProgressDelta],
        *,
        created_ns: Optional[int] = None,
    ) -> None:
        """Persist the exact outbound body before sending it.

        Fails closed when a *different* batch is already pending for the guest:
        the previous exact bytes must be acknowledged or discarded first.
        """
        _require_text("batch_id", batch_id)
        payload = self._check_body(body)
        encoded = _encode_progress(progress)
        ts = now_ns() if created_ns is None else created_ns
        with self._transaction() as conn:
            self._ensure_guest(conn, key, ts)
            rows = list(
                self._execute(
                    conn,
                    "SELECT batch_id FROM pending_batch WHERE cluster = ? AND node = ? AND lxc_id = ?",
                    key.tuple,
                )
            )
            if rows and rows[0]["batch_id"] != batch_id:
                raise CheckpointConflictError(
                    f"pending batch {rows[0]['batch_id']!r} already stored for guest {key}"
                )
            self._execute(
                conn,
                """
                INSERT INTO pending_batch (cluster, node, lxc_id, batch_id, body, created_ns, progress)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (cluster, node, lxc_id) DO UPDATE SET
                    batch_id = excluded.batch_id,
                    body = excluded.body,
                    created_ns = excluded.created_ns,
                    progress = excluded.progress
                """,
                (key.cluster, key.node, key.lxc_id, batch_id, payload, ts, encoded),
            )

    def replace_pending_batch(
        self,
        key: GuestKey,
        batch_id: str,
        body: bytes,
        progress: Sequence[ProgressDelta],
        *,
        created_ns: Optional[int] = None,
    ) -> None:
        """Atomically replace a pending batch's body/timestamps (stale >24h resume)."""
        _require_text("batch_id", batch_id)
        payload = self._check_body(body)
        encoded = _encode_progress(progress)
        ts = now_ns() if created_ns is None else created_ns
        with self._transaction() as conn:
            rows = list(
                self._execute(
                    conn,
                    "SELECT batch_id FROM pending_batch WHERE cluster = ? AND node = ? AND lxc_id = ?",
                    key.tuple,
                )
            )
            if not rows:
                raise CheckpointConflictError(f"no pending batch to replace for guest {key}")
            if rows[0]["batch_id"] != batch_id:
                raise CheckpointConflictError(
                    f"pending batch id mismatch for guest {key}: stored {rows[0]['batch_id']!r}, got {batch_id!r}"
                )
            self._execute(
                conn,
                """
                UPDATE pending_batch SET body = ?, created_ns = ?, progress = ?
                WHERE cluster = ? AND node = ? AND lxc_id = ?
                """,
                (payload, ts, encoded, key.cluster, key.node, key.lxc_id),
            )

    def pending_batch(self, key: GuestKey) -> Optional[PendingBatch]:
        rows = self._read(
            "SELECT * FROM pending_batch WHERE cluster = ? AND node = ? AND lxc_id = ?",
            key.tuple,
        )
        if not rows:
            return None
        row = rows[0]
        raw = _loads_json(row["progress"], "pending_batch.progress")
        if not isinstance(raw, list):
            raise CheckpointCorruptError("pending_batch.progress is not a list")
        return PendingBatch(
            key=key,
            batch_id=row["batch_id"],
            body=bytes(row["body"]),
            created_ns=int(row["created_ns"]),
            progress=[_delta_from_json(item) for item in raw],
        )

    def acknowledge_batch(
        self,
        key: GuestKey,
        batch_id: str,
        *,
        acked_ns: Optional[int] = None,
    ) -> int:
        """Apply every pending delta and drop the batch in one transaction.

        Call only after Loki answered HTTP 204.  Returns the number of source
        progress rows advanced; raises :class:`CheckpointConflictError` (rolling
        back) when the batch id does not match or a delta names an unknown
        source, so a stale/foreign acknowledgement can never advance progress.
        """
        _require_text("batch_id", batch_id)
        ts = now_ns() if acked_ns is None else acked_ns
        with self._transaction() as conn:
            rows = list(
                self._execute(
                    conn,
                    "SELECT batch_id, progress FROM pending_batch WHERE cluster = ? AND node = ? AND lxc_id = ?",
                    key.tuple,
                )
            )
            if not rows:
                raise CheckpointConflictError(f"no pending batch for guest {key}")
            stored = rows[0]
            if stored["batch_id"] != batch_id:
                raise CheckpointConflictError(
                    f"pending batch id mismatch for guest {key}: stored {stored['batch_id']!r}, got {batch_id!r}"
                )
            raw = _loads_json(stored["progress"], "pending_batch.progress")
            if not isinstance(raw, list):
                raise CheckpointCorruptError("pending_batch.progress is not a list")
            deltas = [_delta_from_json(item) for item in raw]
            applied = 0
            for delta in deltas:
                self._source_row(conn, key, delta.source_id)
                self._execute(
                    conn,
                    """
                    UPDATE source SET
                        decoded_offset = ?,
                        line_number = ?,
                        fragment_index = ?,
                        stream_ts_ns = ?,
                        acknowledged = ?,
                        digest = COALESCE(?, digest),
                        updated_ns = ?
                    WHERE cluster = ? AND node = ? AND lxc_id = ? AND source_id = ?
                    """,
                    (
                        delta.decoded_offset,
                        delta.line_number,
                        delta.fragment_index,
                        delta.stream_ts_ns,
                        1 if delta.acknowledged else 0,
                        delta.digest,
                        ts,
                        key.cluster,
                        key.node,
                        key.lxc_id,
                        delta.source_id,
                    ),
                )
                applied += 1
            self._execute(
                conn,
                "DELETE FROM pending_batch WHERE cluster = ? AND node = ? AND lxc_id = ?",
                key.tuple,
            )
            return applied

    def discard_pending_batch(self, key: GuestKey, batch_id: str) -> bool:
        """Drop a pending batch without advancing progress; True when removed."""
        _require_text("batch_id", batch_id)
        with self._transaction() as conn:
            rows = list(
                self._execute(
                    conn,
                    "SELECT batch_id FROM pending_batch WHERE cluster = ? AND node = ? AND lxc_id = ?",
                    key.tuple,
                )
            )
            if not rows:
                return False
            if rows[0]["batch_id"] != batch_id:
                raise CheckpointConflictError(
                    f"pending batch id mismatch for guest {key}: stored {rows[0]['batch_id']!r}, got {batch_id!r}"
                )
            self._execute(
                conn,
                "DELETE FROM pending_batch WHERE cluster = ? AND node = ? AND lxc_id = ?",
                key.tuple,
            )
            return True

    # -- capture intent ----------------------------------------------------- #

    def begin_capture(
        self,
        key: GuestKey,
        capture_id: str,
        manifest: Sequence[Dict[str, Any]],
        expected: Sequence[BlobState],
        *,
        created_ns: Optional[int] = None,
    ) -> CaptureIntent:
        """Commit the frozen initial manifest and its expected spool blobs.

        Called *before* the first byte is copied.  Re-entering with the same
        ``capture_id`` and identical content is idempotent (resume); a different
        manifest under the same id — or a new id while another capture is still
        pending — fails closed.
        """
        _require_text("capture_id", capture_id)
        if not isinstance(manifest, (list, tuple)):
            raise ValueError("manifest must be a sequence of wire file objects")
        for item in manifest:
            if not isinstance(item, dict):
                raise ValueError("manifest entries must be dicts")
        expected_list = list(expected)
        for blob in expected_list:
            if not isinstance(blob, BlobState):
                raise ValueError("expected entries must be BlobState")
            if blob.capture_id != capture_id:
                raise ValueError(f"blob {blob.blob_path!r} belongs to capture {blob.capture_id!r}")
            if blob.captured:
                raise ValueError(f"blob {blob.blob_path!r} must be an uncaptured expectation")
        ts = now_ns() if created_ns is None else created_ns
        manifest_json = _dumps(list(manifest))
        expected_json = _dumps([_blob_json(blob) for blob in expected_list])
        with self._transaction() as conn:
            rows = list(
                self._execute(
                    conn,
                    "SELECT * FROM guest WHERE cluster = ? AND node = ? AND lxc_id = ?",
                    key.tuple,
                )
            )
            existing = rows[0] if rows else None
            if existing is not None and existing["capture_id"] == capture_id:
                if existing["capture_manifest"] != manifest_json or existing["capture_expected"] != expected_json:
                    raise CheckpointConflictError(
                        f"capture {capture_id!r} already committed for guest {key} with different content"
                    )
                for blob in expected_list:
                    self._execute(
                        conn,
                        """
                        INSERT INTO blob (
                            cluster, node, lxc_id, capture_id, blob_path, source_id, source_path,
                            device, inode, compression, high_water_size
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (cluster, node, lxc_id, capture_id, blob_path) DO NOTHING
                        """,
                        (
                            key.cluster,
                            key.node,
                            key.lxc_id,
                            capture_id,
                            blob.blob_path,
                            blob.source_id,
                            blob.source_path,
                            blob.device,
                            blob.inode,
                            blob.compression,
                            blob.high_water_size,
                        ),
                    )
                resumed = self._capture_intent_from_row(existing)
                if resumed is None:  # pragma: no cover - existing row carries capture_id
                    raise CheckpointError(f"capture {capture_id!r} lost its manifest for guest {key}")
                return resumed
            if existing is not None and existing["capture_state"] == "pending":
                raise CheckpointConflictError(
                    f"capture {existing['capture_id']!r} is still pending for guest {key}; "
                    "finish or resume it before starting a new one"
                )
            self._ensure_guest(conn, key, ts)
            self._execute(
                conn,
                """
                UPDATE guest SET capture_id = ?, capture_state = 'pending', capture_manifest = ?,
                    capture_expected = ?, capture_created_ns = ?, updated_ns = ?
                WHERE cluster = ? AND node = ? AND lxc_id = ?
                """,
                (capture_id, manifest_json, expected_json, ts, ts, key.cluster, key.node, key.lxc_id),
            )
            for blob in expected_list:
                self._execute(
                    conn,
                    """
                    INSERT INTO blob (
                        cluster, node, lxc_id, capture_id, blob_path, source_id, source_path,
                        device, inode, compression, high_water_size
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (cluster, node, lxc_id, capture_id, blob_path) DO UPDATE SET
                        source_id = excluded.source_id,
                        source_path = excluded.source_path,
                        device = excluded.device,
                        inode = excluded.inode,
                        compression = excluded.compression,
                        high_water_size = excluded.high_water_size
                    """,
                    (
                        key.cluster,
                        key.node,
                        key.lxc_id,
                        capture_id,
                        blob.blob_path,
                        blob.source_id,
                        blob.source_path,
                        blob.device,
                        blob.inode,
                        blob.compression,
                        blob.high_water_size,
                    ),
                )
            return CaptureIntent(
                capture_id=capture_id,
                state="pending",
                manifest=list(manifest),
                expected=expected_list,
                created_ns=ts,
            )

    def capture_intent(self, key: GuestKey) -> Optional[CaptureIntent]:
        rows = self._read(
            "SELECT * FROM guest WHERE cluster = ? AND node = ? AND lxc_id = ?", key.tuple
        )
        return self._capture_intent_from_row(rows[0]) if rows else None

    def _capture_intent_from_row(self, row: sqlite3.Row) -> Optional[CaptureIntent]:
        if not row["capture_id"]:
            return None
        manifest = _loads_json(row["capture_manifest"], "guest.capture_manifest")
        expected = _loads_json(row["capture_expected"], "guest.capture_expected")
        if not isinstance(manifest, list) or not isinstance(expected, list):
            raise CheckpointCorruptError("capture manifest/expected payload is malformed")
        blobs: List[BlobState] = []
        for item in expected:
            if not isinstance(item, dict):
                raise CheckpointCorruptError("capture expected entry is not an object")
            try:
                blobs.append(BlobState(**item))
            except TypeError as exc:
                raise CheckpointCorruptError(f"capture expected entry invalid: {exc}") from exc
        return CaptureIntent(
            capture_id=row["capture_id"],
            state=row["capture_state"] or "pending",
            manifest=manifest,
            expected=blobs,
            created_ns=int(row["capture_created_ns"] or 0),
        )

    def mark_blob_captured(
        self,
        key: GuestKey,
        capture_id: str,
        blob_path: str,
        *,
        sha256: str,
        captured_bytes: int,
        captured_ns: Optional[int] = None,
    ) -> BlobState:
        """Record a validated blob; the blob must be part of the frozen manifest."""
        _require_text("capture_id", capture_id)
        _require_text("blob_path", blob_path)
        _require_text("sha256", sha256)
        _require_non_negative("captured_bytes", captured_bytes)
        ts = now_ns() if captured_ns is None else captured_ns
        with self._transaction() as conn:
            guest = self._guest_row(conn, key)
            if guest is None or guest["capture_id"] != capture_id:
                raise CheckpointConflictError(
                    f"capture {capture_id!r} is not the active capture for guest {key}"
                )
            if guest["capture_state"] != "pending":
                raise CheckpointConflictError(
                    f"capture {capture_id!r} for guest {key} is {guest['capture_state']!r}, not pending"
                )
            cur = self._execute(
                conn,
                """
                UPDATE blob SET sha256 = ?, captured_bytes = ?, captured_ns = ?
                WHERE cluster = ? AND node = ? AND lxc_id = ? AND capture_id = ? AND blob_path = ?
                """,
                (sha256, captured_bytes, ts, key.cluster, key.node, key.lxc_id, capture_id, blob_path),
            )
            if cur.rowcount == 0:
                raise CheckpointConflictError(
                    f"blob {blob_path!r} was not declared in capture {capture_id!r} for guest {key}"
                )
        blob = self.captured_blob(key, capture_id, blob_path)
        if blob is None:  # pragma: no cover - update above guarantees a row
            raise CheckpointError(f"blob {blob_path!r} vanished after capture update")
        return blob

    def finish_capture(
        self,
        key: GuestKey,
        capture_id: str,
        *,
        state: str = "complete",
        ts_ns: Optional[int] = None,
    ) -> None:
        if state not in ("complete", "failed"):
            raise ValueError("capture state must be 'complete' or 'failed'")
        ts = now_ns() if ts_ns is None else ts_ns
        with self._transaction() as conn:
            guest = self._guest_row(conn, key)
            if guest is None or guest["capture_id"] != capture_id:
                raise CheckpointConflictError(
                    f"capture {capture_id!r} is not the active capture for guest {key}"
                )
            self._execute(
                conn,
                """
                UPDATE guest SET capture_state = ?, updated_ns = ?
                WHERE cluster = ? AND node = ? AND lxc_id = ?
                """,
                (state, ts, key.cluster, key.node, key.lxc_id),
            )

    def captured_blob(self, key: GuestKey, capture_id: str, blob_path: str) -> Optional[BlobState]:
        rows = self._read(
            """
            SELECT * FROM blob WHERE cluster = ? AND node = ? AND lxc_id = ?
                AND capture_id = ? AND blob_path = ?
            """,
            (key.cluster, key.node, key.lxc_id, capture_id, blob_path),
        )
        return _blob_from_row(rows[0]) if rows else None

    def captured_blobs(self, key: GuestKey, capture_id: Optional[str] = None) -> List[BlobState]:
        if capture_id is None:
            rows = self._read(
                "SELECT * FROM blob WHERE cluster = ? AND node = ? AND lxc_id = ? ORDER BY blob_path",
                key.tuple,
            )
        else:
            rows = self._read(
                """
                SELECT * FROM blob WHERE cluster = ? AND node = ? AND lxc_id = ? AND capture_id = ?
                ORDER BY blob_path
                """,
                (key.cluster, key.node, key.lxc_id, capture_id),
            )
        return [_blob_from_row(row) for row in rows]

    def release_blob(
        self,
        key: GuestKey,
        capture_id: str,
        blob_path: str,
        *,
        released_ns: Optional[int] = None,
    ) -> None:
        """Mark a captured blob reclaimable — only after its bytes are acknowledged."""
        _require_text("capture_id", capture_id)
        _require_text("blob_path", blob_path)
        ts = now_ns() if released_ns is None else released_ns
        with self._transaction() as conn:
            cur = self._execute(
                conn,
                """
                UPDATE blob SET released_ns = ?
                WHERE cluster = ? AND node = ? AND lxc_id = ?
                    AND capture_id = ? AND blob_path = ? AND sha256 IS NOT NULL
                """,
                (ts, key.cluster, key.node, key.lxc_id, capture_id, blob_path),
            )
            if cur.rowcount == 0:
                raise CheckpointConflictError(
                    f"blob {blob_path!r} of capture {capture_id!r} is not a captured blob for guest {key}"
                )

    def _guest_row(self, conn: sqlite3.Connection, key: GuestKey) -> Optional[sqlite3.Row]:
        rows = list(
            self._execute(
                conn,
                "SELECT * FROM guest WHERE cluster = ? AND node = ? AND lxc_id = ?",
                key.tuple,
            )
        )
        return rows[0] if rows else None

    # -- delivery verification ---------------------------------------------- #

    def delivery_verification(self, key: GuestKey) -> Optional[DeliveryVerification]:
        rows = self._read(
            "SELECT * FROM guest WHERE cluster = ? AND node = ? AND lxc_id = ?", key.tuple
        )
        return self._delivery_from_row(rows[0]) if rows else None

    def record_delivery_verification(
        self,
        key: GuestKey,
        *,
        effective_alloy_sha256: Optional[str],
        policy_hashes: Dict[str, str],
        journal_marker_ns: Optional[int],
        file_marker_ns: Optional[int],
        verified_ns: Optional[int] = None,
    ) -> None:
        """Persist the marker verification tied to the effective hashes."""
        if not isinstance(policy_hashes, dict):
            raise ValueError("policy_hashes must be a dict")
        for name, value in policy_hashes.items():
            _require_text("policy hash key", name)
            _require_text(f"policy hash {name}", value)
        ts = now_ns() if verified_ns is None else verified_ns
        with self._transaction() as conn:
            self._ensure_guest(conn, key, ts)
            self._execute(
                conn,
                """
                UPDATE guest SET delivery_alloy_sha256 = ?, delivery_policy_hashes = ?,
                    journal_marker_ns = ?, file_marker_ns = ?, delivery_verified_ns = ?, updated_ns = ?
                WHERE cluster = ? AND node = ? AND lxc_id = ?
                """,
                (
                    effective_alloy_sha256,
                    _dumps(policy_hashes),
                    journal_marker_ns,
                    file_marker_ns,
                    ts,
                    ts,
                    key.cluster,
                    key.node,
                    key.lxc_id,
                ),
            )

    @staticmethod
    def _delivery_from_row(row: sqlite3.Row) -> Optional[DeliveryVerification]:
        if row["delivery_verified_ns"] is None and row["delivery_alloy_sha256"] is None:
            return None
        hashes = _loads_json(row["delivery_policy_hashes"], "guest.delivery_policy_hashes")
        if hashes is not None and not isinstance(hashes, dict):
            raise CheckpointCorruptError("guest.delivery_policy_hashes is not an object")
        return DeliveryVerification(
            effective_alloy_sha256=row["delivery_alloy_sha256"],
            policy_hashes=hashes or {},
            journal_marker_ns=row["journal_marker_ns"],
            file_marker_ns=row["file_marker_ns"],
            verified_ns=row["delivery_verified_ns"],
        )

    # -- cache cadence ------------------------------------------------------ #

    def last_cache_clean(self, key: GuestKey, tool: str) -> Optional[int]:
        _require_text("tool", tool)
        rows = self._read(
            """
            SELECT last_success_ns FROM cache_clean
            WHERE cluster = ? AND node = ? AND lxc_id = ? AND tool = ?
            """,
            (key.cluster, key.node, key.lxc_id, tool),
        )
        return int(rows[0]["last_success_ns"]) if rows else None

    def cache_clean_times(self, key: GuestKey) -> Dict[str, int]:
        rows = self._read(
            "SELECT tool, last_success_ns FROM cache_clean WHERE cluster = ? AND node = ? AND lxc_id = ?",
            key.tuple,
        )
        return {row["tool"]: int(row["last_success_ns"]) for row in rows}

    def record_cache_clean(self, key: GuestKey, tool: str, *, ts_ns: Optional[int] = None) -> None:
        """Record a *successful* cache clean (a busy/failed run must not call this)."""
        _require_text("tool", tool)
        ts = now_ns() if ts_ns is None else ts_ns
        with self._transaction() as conn:
            self._ensure_guest(conn, key, ts)
            self._execute(
                conn,
                """
                INSERT INTO cache_clean (cluster, node, lxc_id, tool, last_success_ns)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (cluster, node, lxc_id, tool) DO UPDATE SET
                    last_success_ns = excluded.last_success_ns
                """,
                (key.cluster, key.node, key.lxc_id, tool, ts),
            )

    # -- prune intents ------------------------------------------------------ #

    def record_prune_intent(self, intent: PruneIntent) -> PruneIntent:
        """Persist the recovery intent *before* any quarantine/rename happens."""
        if intent.state != "pending":
            raise ValueError("new prune intents must start in state 'pending'")
        created = intent.created_ns or now_ns()
        with self._transaction() as conn:
            self._ensure_guest(conn, intent.key, created)
            rows = list(
                self._execute(conn, "SELECT * FROM prune_intent WHERE intent_id = ?", (intent.intent_id,))
            )
            if rows:
                stored = _intent_from_row(rows[0])
                if (
                    stored.key != intent.key
                    or stored.path != intent.path
                    or stored.quarantine_path != intent.quarantine_path
                    or stored.inode != intent.inode
                ):
                    raise CheckpointConflictError(
                        f"prune intent {intent.intent_id!r} already exists with different content"
                    )
                return stored
            self._execute(
                conn,
                """
                INSERT INTO prune_intent (
                    intent_id, cluster, node, lxc_id, source_id, path, quarantine_path,
                    device, inode, size, mtime_ns, digest, state, detail, reclaimed_bytes,
                    created_ns, resolved_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, NULL)
                """,
                (
                    intent.intent_id,
                    intent.key.cluster,
                    intent.key.node,
                    intent.key.lxc_id,
                    intent.source_id,
                    intent.path,
                    intent.quarantine_path,
                    intent.device,
                    intent.inode,
                    intent.size,
                    intent.mtime_ns,
                    intent.digest,
                    created,
                ),
            )
        return PruneIntent(**{**_dataclass_kwargs(intent), "created_ns": created})

    def resolve_prune_intent(
        self,
        intent_id: str,
        state: str,
        *,
        reclaimed_bytes: Optional[int] = None,
        detail: Optional[str] = None,
        resolved_ns: Optional[int] = None,
    ) -> None:
        if state not in PRUNE_STATES:
            raise ValueError(f"state must be one of {PRUNE_STATES}")
        if state == "pending":
            raise ValueError("resolve_prune_intent cannot reset an intent to pending")
        if reclaimed_bytes is not None:
            _require_non_negative("reclaimed_bytes", reclaimed_bytes)
        ts = now_ns() if resolved_ns is None else resolved_ns
        with self._transaction() as conn:
            cur = self._execute(
                conn,
                """
                UPDATE prune_intent SET state = ?, reclaimed_bytes = ?, detail = ?, resolved_ns = ?
                WHERE intent_id = ?
                """,
                (state, reclaimed_bytes, detail, ts, intent_id),
            )
            if cur.rowcount == 0:
                raise CheckpointConflictError(f"unknown prune intent {intent_id!r}")

    def prune_intent(self, intent_id: str) -> Optional[PruneIntent]:
        rows = self._read("SELECT * FROM prune_intent WHERE intent_id = ?", (intent_id,))
        return _intent_from_row(rows[0]) if rows else None

    def prune_intents(self, key: Optional[GuestKey] = None) -> List[PruneIntent]:
        if key is None:
            rows = self._read("SELECT * FROM prune_intent ORDER BY created_ns, intent_id")
        else:
            rows = self._read(
                """
                SELECT * FROM prune_intent WHERE cluster = ? AND node = ? AND lxc_id = ?
                ORDER BY created_ns, intent_id
                """,
                key.tuple,
            )
        return [_intent_from_row(row) for row in rows]

    def open_prune_intents(self, key: Optional[GuestKey] = None) -> List[PruneIntent]:
        """Intents still awaiting recovery — replay these before planning more."""
        return [intent for intent in self.prune_intents(key) if intent.state == "pending"]

    def reclaimed_bytes(self, key: Optional[GuestKey] = None) -> int:
        """Sum of measured reclaimed bytes across resolved (done) intents."""
        if key is None:
            rows = self._read(
                "SELECT COALESCE(SUM(reclaimed_bytes), 0) AS total FROM prune_intent WHERE state = 'done'"
            )
        else:
            rows = self._read(
                """
                SELECT COALESCE(SUM(reclaimed_bytes), 0) AS total FROM prune_intent
                WHERE state = 'done' AND cluster = ? AND node = ? AND lxc_id = ?
                """,
                key.tuple,
            )
        return int(rows[0]["total"]) if rows else 0

    # -- tombstone lifecycle ------------------------------------------------ #

    def mark_source_absent(self, key: GuestKey, source_id: str, *, ts_ns: Optional[int] = None) -> bool:
        """Start the 30-day tombstone clock; True when newly marked absent."""
        ts = now_ns() if ts_ns is None else ts_ns
        with self._transaction() as conn:
            row = self._source_row(conn, key, source_id)
            if row["absent_since_ns"] is not None:
                return False
            self._execute(
                conn,
                """
                UPDATE source SET absent_since_ns = ?, updated_ns = ?
                WHERE cluster = ? AND node = ? AND lxc_id = ? AND source_id = ?
                """,
                (ts, ts, key.cluster, key.node, key.lxc_id, source_id),
            )
            return True

    def clear_source_absent(self, key: GuestKey, source_id: str) -> bool:
        """A previously absent source reappeared; True when the tombstone cleared."""
        with self._transaction() as conn:
            row = self._source_row(conn, key, source_id)
            if row["absent_since_ns"] is None:
                return False
            self._execute(
                conn,
                """
                UPDATE source SET absent_since_ns = NULL
                WHERE cluster = ? AND node = ? AND lxc_id = ? AND source_id = ?
                """,
                (key.cluster, key.node, key.lxc_id, source_id),
            )
            return True

    def tombstones(self, key: Optional[GuestKey] = None) -> List[SourceRecord]:
        sql = "SELECT * FROM source WHERE absent_since_ns IS NOT NULL"
        params: Union[Sequence[Any], Dict[str, Any]] = ()
        if key is not None:
            sql += " AND cluster = ? AND node = ? AND lxc_id = ?"
            params = key.tuple
        rows = self._read(sql + " ORDER BY absent_since_ns, source_id", params)
        return [_source_from_row(row) for row in rows]

    def purge_tombstones(
        self,
        *,
        absent_seconds: int = ABSENT_TOMBSTONE_SECONDS,
        now_ts_ns: Optional[int] = None,
    ) -> int:
        """Delete tombstones absent for at least ``absent_seconds``.

        Existing files, unacknowledged history, pending retries, unreleased
        capture ownership, and unresolved prune intents (pending/conflict) that
        still own quarantined bytes are retained regardless of the absence
        window: an intent whose source row vanished would make ownership
        unknowable, so it must keep its row here to recover or Block explicitly.
        """
        if absent_seconds < 0:
            raise ValueError("absent_seconds must be >= 0")
        threshold = (now_ns() if now_ts_ns is None else now_ts_ns) - absent_seconds * 1_000_000_000
        return self._write(
            """
            DELETE FROM source
            WHERE absent_since_ns IS NOT NULL AND absent_since_ns <= ?
                AND acknowledged = 1
                AND NOT EXISTS (
                    SELECT 1 FROM pending_batch
                    WHERE pending_batch.cluster = source.cluster
                        AND pending_batch.node = source.node
                        AND pending_batch.lxc_id = source.lxc_id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM blob
                    WHERE blob.cluster = source.cluster AND blob.node = source.node
                        AND blob.lxc_id = source.lxc_id AND blob.source_id = source.source_id
                        AND blob.released_ns IS NULL
                )
                AND NOT EXISTS (
                    SELECT 1 FROM prune_intent
                    WHERE prune_intent.cluster = source.cluster
                        AND prune_intent.node = source.node
                        AND prune_intent.lxc_id = source.lxc_id
                        AND prune_intent.state NOT IN ('done', 'restored')
                        AND (
                            prune_intent.source_id = source.source_id
                            OR (prune_intent.source_id IS NULL AND prune_intent.path = source.path)
                        )
                )
            """,
            (threshold,),
        )

    # -- internals ---------------------------------------------------------- #

    def _check_body(self, body: bytes) -> bytes:
        if not isinstance(body, (bytes, bytearray, memoryview)):
            raise ValueError("pending body must be bytes")
        data = bytes(body)
        if len(data) > self._pending_body_limit:
            raise CheckpointError(
                f"pending body is {len(data)} bytes; limit is {self._pending_body_limit}"
            )
        return data


def _encode_progress(progress: Sequence[ProgressDelta]) -> str:
    deltas: List[ProgressDelta] = []
    for delta in progress:
        if not isinstance(delta, ProgressDelta):
            raise ValueError("progress entries must be ProgressDelta")
        deltas.append(delta)
    if not deltas:
        raise ValueError("a pending batch must carry at least one progress delta")
    source_ids = [delta.source_id for delta in deltas]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("a pending batch may name each source at most once")
    return _dumps([_delta_to_json(delta) for delta in deltas])


def _blob_json(blob: BlobState) -> Dict[str, Any]:
    return {
        "capture_id": blob.capture_id,
        "source_id": blob.source_id,
        "source_path": blob.source_path,
        "blob_path": blob.blob_path,
        "device": blob.device,
        "inode": blob.inode,
        "compression": blob.compression,
        "high_water_size": blob.high_water_size,
        "sha256": blob.sha256,
        "captured_bytes": blob.captured_bytes,
        "captured_ns": blob.captured_ns,
        "released_ns": blob.released_ns,
    }


def _dataclass_kwargs(instance: Any) -> Dict[str, Any]:
    return {item.name: getattr(instance, item.name) for item in fields(instance)}


__all__ = [
    "ABSENT_TOMBSTONE_SECONDS",
    "CAPTURE_STATES",
    "DB_FILENAME",
    "DEFAULT_PENDING_BODY_LIMIT",
    "PRUNE_STATES",
    "SCHEMA_VERSION",
    "BlobState",
    "CaptureIntent",
    "CheckpointConflictError",
    "CheckpointCorruptError",
    "CheckpointError",
    "CheckpointReadOnlyError",
    "CheckpointSchemaError",
    "CheckpointStore",
    "DeliveryVerification",
    "GuestKey",
    "GuestRecord",
    "InitialManifestState",
    "PendingBatch",
    "ProgressDelta",
    "PruneIntent",
    "SourceIdentity",
    "SourceRecord",
    "checkpoint_path",
    "now_ns",
]
