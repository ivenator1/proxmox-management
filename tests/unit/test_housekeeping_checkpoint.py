"""Regression tests for ``proxmox_fleet.housekeeping_checkpoint``.

The checkpoint is the durability layer behind the acknowledged historical
import: these tests pin the contract behaviours the importer depends on —
atomic pending/ack resume, cluster-qualified keys, capture intent, read-only
dry-run, and fail-closed corruption handling.
"""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from proxmox_fleet.housekeeping_checkpoint import (
    DEFAULT_PENDING_BODY_LIMIT,
    SCHEMA_VERSION,
    BlobState,
    CheckpointConflictError,
    CheckpointCorruptError,
    CheckpointError,
    CheckpointReadOnlyError,
    CheckpointSchemaError,
    CheckpointStore,
    GuestKey,
    ProgressDelta,
    PruneIntent,
    SourceIdentity,
    checkpoint_path,
)

DAY_NS = 86_400 * 1_000_000_000


def _key(cluster: str = "prod", node: str = "pve-01", lxc_id: str = "123") -> GuestKey:
    return GuestKey(cluster=cluster, node=node, lxc_id=lxc_id)


def _identity(**kw) -> SourceIdentity:
    base = dict(
        path="/data/logs/access.log",
        device=44,
        inode=98765,
        size=4096,
        mtime_ns=1_700_000_000_000_000_000,
        allocated_bytes=8192,
        compression="plain",
        profile="npm",
        log_kind="application",
        is_active=False,
    )
    base.update(kw)
    return SourceIdentity(**base)


def _delta(source_id: str = "src-1", **kw) -> ProgressDelta:
    base = dict(
        source_id=source_id,
        decoded_offset=1024,
        line_number=7,
        fragment_index=0,
        stream_ts_ns=1_700_000_000_123_456_789,
        acknowledged=True,
    )
    base.update(kw)
    return ProgressDelta(**base)


def _store(tmp_path: Path, **kw) -> CheckpointStore:
    return CheckpointStore.open(tmp_path / "housekeeping.sqlite3", **kw)


def _expected_blob(capture_id: str = "cap-1", **kw) -> BlobState:
    base = dict(
        capture_id=capture_id,
        source_id="src-1",
        source_path="/data/logs/access.log",
        blob_path="/var/tmp/fleet-log-import/abc/cap-1/blob-000",
        device=44,
        inode=98765,
        compression="plain",
        high_water_size=4096,
    )
    base.update(kw)
    return BlobState(**base)


# --------------------------------------------------------------------------- #
# Schema lifecycle
# --------------------------------------------------------------------------- #


def test_creates_schema_with_user_version(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(path) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.is_empty is False
        assert store.read_only is False
        assert store.guests() == []
    raw = sqlite3.connect(str(path))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        raw.close()
    assert {"guest", "source", "blob", "pending_batch", "prune_intent", "cache_clean"} <= tables


def test_checkpoint_path_helper(tmp_path):
    assert checkpoint_path(tmp_path) == tmp_path / "housekeeping.sqlite3"


def test_reopen_preserves_state(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(path) as store:
        store.record_source(_key(), "src-1", _identity(), provenance={"path": "/data/logs/access.log"})
    with CheckpointStore.open(path) as store:
        record = store.source(_key(), "src-1")
    assert record is not None
    assert record.provenance == {"path": "/data/logs/access.log"}
    assert record.decoded_offset == 0


# --------------------------------------------------------------------------- #
# Sources, aliases, digests
# --------------------------------------------------------------------------- #


def test_record_source_round_trip(tmp_path):
    with _store(tmp_path) as store:
        record = store.record_source(
            _key(),
            "src-1",
            _identity(),
            provenance={"path": "/data/logs/access.log", "bytes": 4096},
            aliases=[{"path": "/data/logs/access.log.1", "device": 44, "inode": 98765}],
            in_initial_manifest=True,
            capture_id="cap-1",
            blob_path="/var/tmp/fleet-log-import/abc/cap-1/blob-000",
        )
        assert record.acknowledged is False
        assert record.absent is False
        assert record.in_initial_manifest is True
        assert record.capture_id == "cap-1"
        assert record.aliases[0]["path"] == "/data/logs/access.log.1"
        assert store.source(_key(), "src-1") == record


def test_record_source_preserves_progress_until_reset(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        batch = [_delta()]
        store.save_pending_batch(_key(), "b1", b"{}", batch)
        store.acknowledge_batch(_key(), "b1")
        assert store.source(_key(), "src-1").decoded_offset == 1024
        # Append-only refresh keeps progress.
        refreshed = store.record_source(_key(), "src-1", _identity(size=8192))
        assert refreshed.decoded_offset == 1024
        assert refreshed.size == 8192
        # A replaced/truncated inode is a new archive input.
        reset = store.record_source(_key(), "src-1", _identity(size=10, inode=98766), reset_progress=True)
        assert reset.decoded_offset == 0
        assert reset.line_number == 0
        assert reset.acknowledged is False


def test_aliases_dedupe_and_unknown_source(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        alias = {"path": "/data/logs/access.log.2.gz", "device": 44, "inode": 98765, "compression": "gzip"}
        assert store.add_source_alias(_key(), "src-1", alias) is True
        assert store.add_source_alias(_key(), "src-1", dict(alias)) is False
        assert store.source(_key(), "src-1").aliases == [alias]
        with pytest.raises(CheckpointConflictError):
            store.add_source_alias(_key(), "missing", alias)
        with pytest.raises(CheckpointConflictError):
            store.set_source_digest(_key(), "missing", "a" * 64)


def test_set_digest_and_capture(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.set_source_digest(_key(), "src-1", "ab" * 32)
        store.set_source_capture(_key(), "src-1", capture_id="cap-1", blob_path="/spool/blob-000")
        record = store.source(_key(), "src-1")
        assert record.digest == "ab" * 32
        assert (record.capture_id, record.blob_path) == ("cap-1", "/spool/blob-000")


def test_source_identity_validation():
    with pytest.raises(ValueError):
        _identity(compression="lz4")
    with pytest.raises(ValueError):
        _identity(profile="nginx")
    with pytest.raises(ValueError):
        _identity(log_kind="audit")
    with pytest.raises(ValueError):
        _identity(size=-1)
    with pytest.raises(ValueError):
        _identity(is_active=1)
    with pytest.raises(ValueError):
        GuestKey(cluster="", node="n", lxc_id="1")


# --------------------------------------------------------------------------- #
# Pending batch: persist -> ack, atomicity, resume
# --------------------------------------------------------------------------- #


def test_pending_batch_round_trip_and_ack(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        body = b'{"streams":[]}'
        store.save_pending_batch(_key(), "b1", body, [_delta()])
        pending = store.pending_batch(_key())
        assert pending is not None
        assert pending.batch_id == "b1"
        assert pending.body == body
        assert pending.progress == [_delta()]
        assert store.source(_key(), "src-1").acknowledged is False

        assert store.acknowledge_batch(_key(), "b1") == 1
        assert store.pending_batch(_key()) is None
        record = store.source(_key(), "src-1")
        assert record.decoded_offset == 1024
        assert record.line_number == 7
        assert record.stream_ts_ns == 1_700_000_000_123_456_789
        assert record.acknowledged is True


def test_ack_mismatched_batch_id_is_conflict_and_preserves_state(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.save_pending_batch(_key(), "b1", b"{}", [_delta()])
        with pytest.raises(CheckpointConflictError):
            store.acknowledge_batch(_key(), "b2")
        assert store.pending_batch(_key()) is not None
        assert store.source(_key(), "src-1").decoded_offset == 0


def test_ack_unknown_source_rolls_back_entirely(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        deltas = [_delta("src-1"), _delta("ghost", decoded_offset=5)]
        store.save_pending_batch(_key(), "b1", b"{}", deltas)
        with pytest.raises(CheckpointConflictError):
            store.acknowledge_batch(_key(), "b1")
        # Nothing advanced and the exact pending bytes are retained.
        assert store.source(_key(), "src-1").decoded_offset == 0
        assert store.pending_batch(_key()).body == b"{}"


def test_ack_records_partial_progress_then_digest_on_completion(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity(size=8192), in_initial_manifest=True)
        # First batch covers only the head of the file: not yet fully acknowledged.
        store.save_pending_batch(
            _key(), "b1", b"partial", [_delta(decoded_offset=4096, acknowledged=False, digest="aa" * 32)]
        )
        store.acknowledge_batch(_key(), "b1")
        record = store.source(_key(), "src-1")
        assert (record.decoded_offset, record.acknowledged, record.digest) == (4096, False, "aa" * 32)
        assert store.initial_manifest_state(_key()).remaining == 1
        # Second batch completes it and records the final prefix digest.
        store.save_pending_batch(
            _key(),
            "b2",
            b"final",
            [_delta(decoded_offset=8192, line_number=99, acknowledged=True, digest="bb" * 32)],
        )
        store.acknowledge_batch(_key(), "b2")
        record = store.source(_key(), "src-1")
        assert (record.decoded_offset, record.acknowledged, record.digest) == (8192, True, "bb" * 32)
        state = store.initial_manifest_state(_key())
        assert (state.total, state.acknowledged, state.remaining, state.pending_batch) == (1, 1, 0, False)


def test_initial_manifest_state_reports_pending_batch_and_non_manifest_sources(tmp_path):
    with _store(tmp_path) as store:
        empty = store.initial_manifest_state(_key())
        assert (empty.capture_id, empty.capture_state) == (None, None)
        assert (empty.total, empty.acknowledged, empty.remaining, empty.pending_batch) == (0, 0, 0, False)
        store.record_source(_key(), "frozen", _identity(), in_initial_manifest=True)
        store.record_source(_key(), "late", _identity(path="/data/logs/error.log", inode=2))
        state = store.initial_manifest_state(_key())
        assert (state.total, state.remaining) == (1, 1)
        store.save_pending_batch(_key(), "b1", b"{}", [_delta("frozen")])
        assert store.initial_manifest_state(_key()).pending_batch is True
        store.acknowledge_batch(_key(), "b1")
        state = store.initial_manifest_state(_key())
        assert (state.acknowledged, state.remaining, state.pending_batch) == (1, 0, False)


def test_ack_without_pending_batch_is_conflict(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        with pytest.raises(CheckpointConflictError):
            store.acknowledge_batch(_key(), "b1")


def test_pending_batch_survives_reopen_and_resumes(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.save_pending_batch(_key(), "b1", b"exact-bytes", [_delta()])
    # "crash" before the 204: reopen and acknowledge the same batch.
    with CheckpointStore.open(path) as store:
        pending = store.pending_batch(_key())
        assert pending is not None and pending.body == b"exact-bytes"
        store.acknowledge_batch(_key(), "b1")
    with CheckpointStore.open(path) as store:
        assert store.pending_batch(_key()) is None
        assert store.source(_key(), "src-1").decoded_offset == 1024


def test_save_conflicting_batch_while_pending_fails_closed(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.save_pending_batch(_key(), "b1", b"first", [_delta()])
        with pytest.raises(CheckpointConflictError):
            store.save_pending_batch(_key(), "b2", b"second", [_delta()])
        # Re-saving the same batch id is a harmless retry.
        store.save_pending_batch(_key(), "b1", b"first", [_delta()])
        assert store.pending_batch(_key()).body == b"first"


def test_replace_pending_batch_requires_matching_id(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.save_pending_batch(_key(), "b1", b"first", [_delta()], created_ns=1)
        with pytest.raises(CheckpointConflictError):
            store.replace_pending_batch(_key(), "b2", b"second", [_delta()])
        store.replace_pending_batch(_key(), "b1", b"second", [_delta(stream_ts_ns=2)], created_ns=2)
        pending = store.pending_batch(_key())
        assert pending.body == b"second"
        assert pending.created_ns == 2
        assert pending.progress[0].stream_ts_ns == 2


def test_replace_without_pending_is_conflict(tmp_path):
    with _store(tmp_path) as store:
        with pytest.raises(CheckpointConflictError):
            store.replace_pending_batch(_key(), "b1", b"x", [_delta()])


def test_discard_pending_batch(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.save_pending_batch(_key(), "b1", b"x", [_delta()])
        assert store.discard_pending_batch(_key(), "b1") is True
        assert store.pending_batch(_key()) is None
        assert store.discard_pending_batch(_key(), "b1") is False
        store.save_pending_batch(_key(), "b2", b"y", [_delta()])
        with pytest.raises(CheckpointConflictError):
            store.discard_pending_batch(_key(), "b1")


def test_body_limit_and_progress_validation(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        with pytest.raises(CheckpointError):
            store.save_pending_batch(_key(), "b1", b"x" * (DEFAULT_PENDING_BODY_LIMIT + 1), [_delta()])
        assert store.pending_batch(_key()) is None
        with pytest.raises(ValueError):
            store.save_pending_batch(_key(), "b1", b"{}", [])
        with pytest.raises(ValueError):
            store.save_pending_batch(_key(), "b1", b"{}", [_delta("src-1"), _delta("src-1")])
        with pytest.raises(ValueError):
            store.save_pending_batch(_key(), "b1", b"{}", ["not-a-delta"])
        store.save_pending_batch(_key(), "b1", b"x" * DEFAULT_PENDING_BODY_LIMIT, [_delta()])


def test_pending_body_accepts_bytes_like(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.save_pending_batch(_key(), "b1", bytearray(b"body"), [_delta()])
        assert store.pending_batch(_key()).body == b"body"
        store.replace_pending_batch(_key(), "b1", memoryview(b"body2"), [_delta()])
        assert store.pending_batch(_key()).body == b"body2"
        with pytest.raises(ValueError):
            store.save_pending_batch(_key(), "b2", "not-bytes", [_delta()])


def test_progress_delta_validation():
    with pytest.raises(ValueError):
        _delta(stream_ts_ns=0)
    with pytest.raises(ValueError):
        _delta(decoded_offset=-1)
    with pytest.raises(ValueError):
        _delta(source_id="")
    with pytest.raises(ValueError):
        _delta(acknowledged="yes")


def test_pending_batch_limit_is_constructor_configurable(tmp_path):
    with CheckpointStore.open(tmp_path / "db.sqlite3", pending_body_limit=8) as store:
        store.record_source(_key(), "src-1", _identity())
        with pytest.raises(CheckpointError):
            store.save_pending_batch(_key(), "b1", b"123456789", [_delta()])
    with pytest.raises(ValueError):
        CheckpointStore.open(tmp_path / "db.sqlite3", pending_body_limit=0)


# --------------------------------------------------------------------------- #
# Cluster-qualified keys never collide
# --------------------------------------------------------------------------- #


def test_cluster_colliding_ids_are_distinct(tmp_path):
    alpha = _key(cluster="alpha")
    beta = _key(cluster="beta")
    with _store(tmp_path) as store:
        store.record_source(alpha, "src-1", _identity(path="/data/logs/alpha.log"))
        store.record_source(beta, "src-1", _identity(path="/data/logs/beta.log"))
        store.save_pending_batch(alpha, "batch-alpha", b"alpha", [_delta()])
        store.save_pending_batch(beta, "batch-beta", b"beta", [_delta()])
        assert store.source(alpha, "src-1").path == "/data/logs/alpha.log"
        assert store.source(beta, "src-1").path == "/data/logs/beta.log"
        assert store.pending_batch(alpha).body == b"alpha"
        assert store.pending_batch(beta).body == b"beta"
        assert set(store.guests()) == {alpha, beta}
        store.acknowledge_batch(alpha, "batch-alpha")
        assert store.pending_batch(beta).body == b"beta"
        assert store.source(beta, "src-1").decoded_offset == 0


def test_delete_guest_only_removes_that_guest(tmp_path):
    alpha = _key(cluster="alpha")
    beta = _key(cluster="beta")
    with _store(tmp_path) as store:
        store.record_source(alpha, "src-1", _identity())
        store.record_source(beta, "src-1", _identity())
        store.record_cache_clean(alpha, "apt")
        store.record_prune_intent(
            PruneIntent(
                intent_id="pi-1",
                key=alpha,
                path="/data/logs/access.log.3.gz",
                quarantine_path="/data/logs/.fleet-housekeeping-quarantine/access.log.3.gz",
                device=44,
                inode=1,
                size=10,
                mtime_ns=1,
            )
        )
        store.delete_guest(alpha)
        assert store.guests() == [beta]
        assert store.source(alpha, "src-1") is None
        assert store.sources(beta) != []
        assert store.prune_intents(alpha) == []


# --------------------------------------------------------------------------- #
# Capture intent
# --------------------------------------------------------------------------- #


def test_capture_intent_committed_before_capture_and_resumable(tmp_path):
    with _store(tmp_path) as store:
        manifest = [{"path": "/data/logs/access.log", "device": 44, "inode": 98765, "size": 4096}]
        intent = store.begin_capture(_key(), "cap-1", manifest, [_expected_blob()], created_ns=123)
        assert intent.state == "pending"
        assert store.capture_intent(_key()).created_ns == 123
        # Idempotent resume with identical content.
        again = store.begin_capture(_key(), "cap-1", manifest, [_expected_blob()])
        assert again.capture_id == "cap-1"
        assert len(store.captured_blobs(_key(), "cap-1")) == 1
        # Same capture id with a different manifest is a conflict.
        with pytest.raises(CheckpointConflictError):
            store.begin_capture(_key(), "cap-1", manifest + [{"path": "/x"}], [_expected_blob()])
        # A different capture id while one is pending is a conflict.
        with pytest.raises(CheckpointConflictError):
            store.begin_capture(_key(), "cap-2", manifest, [_expected_blob(capture_id="cap-2")])


def test_mark_blob_captured_requires_expected_blob(tmp_path):
    with _store(tmp_path) as store:
        store.begin_capture(_key(), "cap-1", [], [_expected_blob()])
        with pytest.raises(CheckpointConflictError):
            store.mark_blob_captured(
                _key(), "cap-1", "/spool/not-declared", sha256="a" * 64, captured_bytes=1
            )
        blob = store.mark_blob_captured(
            _key(),
            "cap-1",
            "/var/tmp/fleet-log-import/abc/cap-1/blob-000",
            sha256="b" * 64,
            captured_bytes=4096,
            captured_ns=456,
        )
        assert blob.captured is True
        assert blob.captured_ns == 456
        assert store.captured_blob(_key(), "cap-1", blob.blob_path) == blob


def test_blob_capture_after_finish_and_release(tmp_path):
    with _store(tmp_path) as store:
        blob = _expected_blob()
        store.begin_capture(_key(), "cap-1", [], [blob])
        with pytest.raises(CheckpointConflictError):
            store.release_blob(_key(), "cap-1", blob.blob_path)  # not captured yet
        store.mark_blob_captured(_key(), "cap-1", blob.blob_path, sha256="c" * 64, captured_bytes=4096)
        store.finish_capture(_key(), "cap-1")
        assert store.capture_intent(_key()).state == "complete"
        with pytest.raises(CheckpointConflictError):
            store.mark_blob_captured(_key(), "cap-1", blob.blob_path, sha256="d" * 64, captured_bytes=1)
        store.release_blob(_key(), "cap-1", blob.blob_path, released_ns=999)
        assert store.captured_blob(_key(), "cap-1", blob.blob_path).released_ns == 999


def test_begin_capture_validates_expected(tmp_path):
    with _store(tmp_path) as store:
        with pytest.raises(ValueError):
            store.begin_capture(_key(), "cap-1", [], [_expected_blob(capture_id="cap-9")])
        with pytest.raises(ValueError):
            store.begin_capture(
                _key(), "cap-1", [], [_expected_blob(sha256="a" * 64, captured_ns=1)]
            )
        with pytest.raises(ValueError):
            store.begin_capture(_key(), "cap-1", ["not-a-dict"], [])


def test_finish_capture_unknown_id_conflicts(tmp_path):
    with _store(tmp_path) as store:
        store.begin_capture(_key(), "cap-1", [], [])
        with pytest.raises(CheckpointConflictError):
            store.finish_capture(_key(), "cap-2")
        with pytest.raises(ValueError):
            store.finish_capture(_key(), "cap-1", state="pending")


# --------------------------------------------------------------------------- #
# Delivery verification + cache cadence
# --------------------------------------------------------------------------- #


def test_delivery_verification_round_trip(tmp_path):
    with _store(tmp_path) as store:
        assert store.delivery_verification(_key()) is None
        store.record_delivery_verification(
            _key(),
            effective_alloy_sha256="ab" * 32,
            policy_hashes={"journald": "cd" * 32, "alloy_env": "ef" * 32},
            journal_marker_ns=111,
            file_marker_ns=222,
            verified_ns=333,
        )
        verification = store.delivery_verification(_key())
        assert verification.effective_alloy_sha256 == "ab" * 32
        assert verification.policy_hashes == {"journald": "cd" * 32, "alloy_env": "ef" * 32}
        assert (verification.journal_marker_ns, verification.file_marker_ns) == (111, 222)
        assert verification.verified_ns == 333
        store.record_delivery_verification(
            _key(),
            effective_alloy_sha256="99" * 32,
            policy_hashes={},
            journal_marker_ns=None,
            file_marker_ns=None,
        )
        assert store.delivery_verification(_key()).effective_alloy_sha256 == "99" * 32


def test_cache_cadence_per_tool(tmp_path):
    with _store(tmp_path) as store:
        assert store.last_cache_clean(_key(), "yarn") is None
        assert store.cache_clean_times(_key()) == {}
        store.record_cache_clean(_key(), "yarn", ts_ns=1000)
        store.record_cache_clean(_key(), "apt", ts_ns=2000)
        assert store.last_cache_clean(_key(), "yarn") == 1000
        assert store.cache_clean_times(_key()) == {"yarn": 1000, "apt": 2000}
        # A successful retry advances only that tool.
        store.record_cache_clean(_key(), "yarn", ts_ns=3000)
        assert store.last_cache_clean(_key(), "yarn") == 3000
        assert store.last_cache_clean(_key(), "apt") == 2000
        with pytest.raises(ValueError):
            store.last_cache_clean(_key(), "")


# --------------------------------------------------------------------------- #
# Prune intents
# --------------------------------------------------------------------------- #


def _intent(intent_id: str = "pi-1", **kw) -> PruneIntent:
    base = dict(
        intent_id=intent_id,
        key=_key(),
        path="/data/logs/access.log.3.gz",
        quarantine_path="/data/logs/.fleet-housekeeping-quarantine/access.log.3.gz",
        device=44,
        inode=555,
        size=1024,
        mtime_ns=1_700_000_000_000_000_000,
        source_id="src-1",
        digest="a" * 64,
    )
    base.update(kw)
    return PruneIntent(**base)


def test_prune_intent_record_recover_resolve(tmp_path):
    with _store(tmp_path) as store:
        stored = store.record_prune_intent(_intent())
        assert stored.state == "pending"
        assert stored.created_ns > 0
        # Recovery replay: unfinished intent is visible before new planning.
        assert [i.intent_id for i in store.open_prune_intents(_key())] == ["pi-1"]
        # Idempotent re-record of identical content returns the stored row.
        assert store.record_prune_intent(_intent()).state == "pending"
        with pytest.raises(CheckpointConflictError):
            store.record_prune_intent(_intent(inode=999))
        store.resolve_prune_intent("pi-1", "done", reclaimed_bytes=4096, resolved_ns=777)
        resolved = store.prune_intent("pi-1")
        assert (resolved.state, resolved.reclaimed_bytes, resolved.resolved_ns) == ("done", 4096, 777)
        assert store.open_prune_intents(_key()) == []
        assert store.reclaimed_bytes(_key()) == 4096
        assert store.reclaimed_bytes() == 4096


def test_prune_intent_validation_and_missing(tmp_path):
    with _store(tmp_path) as store:
        with pytest.raises(ValueError):
            store.record_prune_intent(_intent(state="done"))
        with pytest.raises(CheckpointConflictError):
            store.resolve_prune_intent("nope", "done")
        with pytest.raises(ValueError):
            store.resolve_prune_intent("nope", "pending")
        with pytest.raises(ValueError):
            store.resolve_prune_intent("nope", "bogus")


def test_prune_intents_are_guest_scoped(tmp_path):
    alpha = _key(cluster="alpha")
    with _store(tmp_path) as store:
        store.record_prune_intent(_intent("pi-a", key=alpha))
        store.record_prune_intent(_intent("pi-b", key=_key(cluster="beta")))
        assert [i.intent_id for i in store.prune_intents(alpha)] == ["pi-a"]
        assert len(store.prune_intents()) == 2


# --------------------------------------------------------------------------- #
# Tombstones
# --------------------------------------------------------------------------- #


def test_tombstone_lifecycle_and_30_day_purge(tmp_path):
    now = time.time_ns()
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.mark_source_acknowledged(_key(), "src-1")
        store.record_source(_key(), "src-2", _identity(path="/data/logs/error.log", inode=2))
        assert store.mark_source_absent(_key(), "src-1", ts_ns=now) is True
        assert store.mark_source_absent(_key(), "src-1", ts_ns=now) is False
        assert [t.source_id for t in store.tombstones(_key())] == ["src-1"]
        # 29 days: untouched.
        assert store.purge_tombstones(now_ts_ns=now + 29 * DAY_NS) == 0
        assert store.source(_key(), "src-1") is not None
        # Present file is never purged even with a huge window.
        assert store.purge_tombstones(absent_seconds=0, now_ts_ns=now + 100 * DAY_NS) == 1
        assert store.source(_key(), "src-1") is None
        assert store.source(_key(), "src-2") is not None


def test_tombstone_cleared_when_source_reappears(tmp_path):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.mark_source_absent(_key(), "src-1")
        assert store.tombstones(_key()) != []
        assert store.clear_source_absent(_key(), "src-1") is True
        assert store.clear_source_absent(_key(), "src-1") is False
        assert store.tombstones(_key()) == []
        store.mark_source_absent(_key(), "src-1")
        # A refresh of a reappeared source also clears the tombstone.
        store.record_source(_key(), "src-1", _identity())
        assert store.tombstones(_key()) == []
        with pytest.raises(CheckpointConflictError):
            store.mark_source_absent(_key(), "ghost")



def test_purge_tombstones_rejects_negative_window(tmp_path):
    with _store(tmp_path) as store:
        with pytest.raises(ValueError):
            store.purge_tombstones(absent_seconds=-1)


# --------------------------------------------------------------------------- #
# Read-only dry-run
# --------------------------------------------------------------------------- #


def test_read_only_missing_db_is_empty_view_and_creates_nothing(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(path, read_only=True) as store:
        assert store.read_only is True
        assert store.is_empty is True
        assert store.guests() == []
        assert store.pending_batch(_key()) is None
        assert store.capture_intent(_key()) is None
        assert store.tombstones() == []
        assert store.prune_intents() == []
        assert store.reclaimed_bytes() == 0
        assert store.delivery_verification(_key()) is None
        assert store.last_cache_clean(_key(), "apt") is None
        with pytest.raises(CheckpointReadOnlyError):
            store.record_source(_key(), "src-1", _identity())
        with pytest.raises(CheckpointReadOnlyError):
            store.save_pending_batch(_key(), "b1", b"{}", [_delta()])
        with pytest.raises(CheckpointReadOnlyError):
            store.acknowledge_batch(_key(), "b1")
        with pytest.raises(CheckpointReadOnlyError):
            store.record_cache_clean(_key(), "apt")
        with pytest.raises(CheckpointReadOnlyError):
            store.record_prune_intent(_intent())
        with pytest.raises(CheckpointReadOnlyError):
            store.mark_source_absent(_key(), "src-1")
        with pytest.raises(CheckpointReadOnlyError):
            store.delete_guest(_key())
        with pytest.raises(CheckpointReadOnlyError):
            store.purge_tombstones()
    assert list(tmp_path.iterdir()) == []


def test_read_only_existing_db_reads_and_rejects_writes(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.record_cache_clean(_key(), "yarn", ts_ns=42)
    before = path.read_bytes()
    with CheckpointStore.open(path, read_only=True) as store:
        assert store.is_empty is False
        assert store.source(_key(), "src-1") is not None
        assert store.last_cache_clean(_key(), "yarn") == 42
        with pytest.raises(CheckpointReadOnlyError):
            store.record_cache_clean(_key(), "apt")
        store.close()
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["housekeeping.sqlite3"]


def test_read_only_handle_is_usable_after_close(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(path) as store:
        store.record_source(_key(), "src-1", _identity())
    store = CheckpointStore.open(path, read_only=True)
    assert store.source(_key(), "src-1") is not None
    store.close()
    # After close the handle degrades to an empty view rather than raising.
    assert store.is_empty is True
    assert store.guests() == []


# --------------------------------------------------------------------------- #
# Corruption / schema mismatch: raise, never reset
# --------------------------------------------------------------------------- #


def test_garbage_file_raises_corrupt_and_is_untouched(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    path.write_bytes(b"this is not a sqlite database at all\n" * 64)
    before = path.read_bytes()
    for read_only in (True, False):
        with pytest.raises(CheckpointCorruptError):
            CheckpointStore.open(path, read_only=read_only)
    assert path.read_bytes() == before


def test_future_schema_version_raises_and_is_untouched(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(path):
        pass
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()
    before = path.read_bytes()
    for read_only in (True, False):
        with pytest.raises(CheckpointSchemaError):
            CheckpointStore.open(path, read_only=read_only)
    assert path.read_bytes() == before


def test_user_version_zero_with_tables_raises(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(path):
        pass
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA user_version = 0")
    raw.commit()
    raw.close()
    with pytest.raises(CheckpointSchemaError):
        CheckpointStore.open(path)
    with pytest.raises(CheckpointSchemaError):
        CheckpointStore.open(path, read_only=True)


def test_missing_required_table_raises(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(path):
        pass
    raw = sqlite3.connect(str(path))
    raw.execute("DROP TABLE cache_clean")
    raw.commit()
    raw.close()
    with pytest.raises(CheckpointSchemaError):
        CheckpointStore.open(path)
    with pytest.raises(CheckpointSchemaError):
        CheckpointStore.open(path, read_only=True)


def test_writable_open_does_not_wipe_existing_rows_on_schema_error(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(path) as store:
        store.record_source(_key(), "src-1", _identity())
    raw = sqlite3.connect(str(path))
    raw.execute("PRAGMA user_version = 7")
    raw.commit()
    raw.close()
    with pytest.raises(CheckpointSchemaError):
        CheckpointStore.open(path)
    raw = sqlite3.connect(str(path))
    try:
        assert raw.execute("SELECT COUNT(*) FROM source").fetchone()[0] == 1
    finally:
        raw.close()


def test_corrupt_progress_payload_is_reported(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.save_pending_batch(_key(), "b1", b"{}", [_delta()])
    raw = sqlite3.connect(str(path))
    raw.execute("UPDATE pending_batch SET progress = ?", (json.dumps({"not": "a list"}),))
    raw.commit()
    raw.close()
    with CheckpointStore.open(path) as store:
        with pytest.raises(CheckpointCorruptError):
            store.pending_batch(_key())


def test_source_row_readable_after_store_close(tmp_path):
    path = tmp_path / "housekeeping.sqlite3"
    store = CheckpointStore.open(path)
    store.record_source(_key(), "src-1", _identity())
    store.close()
    with CheckpointStore.open(path, read_only=True) as ro:
        assert ro.source(_key(), "src-1").path == "/data/logs/access.log"


@pytest.mark.parametrize("protection", ["unacknowledged", "pending", "unreleased", "prune_intent"])
def test_absence_never_discards_unacknowledged_or_owned_capture_progress(tmp_path, protection):
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        if protection != "unacknowledged":
            store.mark_source_acknowledged(_key(), "src-1")
        if protection == "pending":
            store.save_pending_batch(_key(), "retry", b"{}", [_delta()])
        if protection == "unreleased":
            store.begin_capture(_key(), "cap-1", [], [_expected_blob()])
        if protection == "prune_intent":
            store.record_prune_intent(
                _intent("pi-1", source_id="src-1", path="/data/logs/access.log")
            )
        store.mark_source_absent(_key(), "src-1", ts_ns=1)
        assert store.purge_tombstones(now_ts_ns=100 * DAY_NS) == 0
        assert store.source(_key(), "src-1") is not None


def test_purge_tombstones_retains_unresolved_prune_intent_then_releases(tmp_path):
    now = time.time_ns()
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.mark_source_acknowledged(_key(), "src-1")
        store.mark_source_absent(_key(), "src-1", ts_ns=now)
        # A NULL-source_id intent naming the same path still owns the row.
        store.record_prune_intent(
            _intent("pi-path", source_id=None, path="/data/logs/access.log")
        )
        assert store.purge_tombstones(now_ts_ns=now + 100 * DAY_NS) == 0
        # A conflicting (unresolved) quarantine also retains ownership...
        store.resolve_prune_intent("pi-path", "conflict")
        assert store.purge_tombstones(now_ts_ns=now + 100 * DAY_NS) == 0
        assert store.source(_key(), "src-1") is not None
        # ...until the ownership question is actually resolved.
        store.resolve_prune_intent("pi-path", "restored")
        assert store.purge_tombstones(now_ts_ns=now + 100 * DAY_NS) == 1
        assert store.source(_key(), "src-1") is None


def test_purge_tombstones_releases_after_intent_resolved_by_source_id(tmp_path):
    now = time.time_ns()
    with _store(tmp_path) as store:
        store.record_source(_key(), "src-1", _identity())
        store.mark_source_acknowledged(_key(), "src-1")
        store.mark_source_absent(_key(), "src-1", ts_ns=now)
        store.record_prune_intent(_intent("pi-1", source_id="src-1"))
        assert store.purge_tombstones(now_ts_ns=now + 100 * DAY_NS) == 0
        store.resolve_prune_intent("pi-1", "done", reclaimed_bytes=2048)
        assert store.purge_tombstones(now_ts_ns=now + 100 * DAY_NS) == 1
        assert store.source(_key(), "src-1") is None
