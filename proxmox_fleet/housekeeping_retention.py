"""Local-retention, delivery verification and prune *policy* for one guest.

This module owns the retention slice's safety decisions and sequencing:

* prove real delivery before any tightening or deletion: a unique ``logger``
  journal marker and a genuine profile file marker (a disposable NPM ``.log``
  under ``/data/logs``, or a read-only localhost HTTPS request whose path/UA
  token lands in the native PBS access log) must be queryable from the
  configured Loki endpoint with the live identity labels and the packed
  ``filename``;
* reconcile the fleet-owned journald drop-in and the managed NPM hourly policy
  through ``housekeeping_native``, restart journald/logrotate only on drift,
  then rotate/vacuum archived journals — never by unlinking ``.journal`` files;
* prune only files the acknowledged importer already proved complete and whose
  identity/digest/writer state still matches, by persisting an exact quarantine
  intent *before* the destructive call and resolving the measured outcome.

The exact native config bytes, logrotate parsing and bounded owned-file
read/write commands live in ``proxmox_fleet.housekeeping_native``; this module
decides *whether* to run them.  Everything here is policy on the manager; the
guest/node helper only transports.

Gate: configuration tightening, journal vacuum and file pruning all require the
initial manifest to be fully acknowledged (capture complete, no pending push
batch), a successful ``/ready``, confirmed ``alloy`` read access for recognized
profiles, and a matching real delivery verification.  File deletion additionally
requires the importer's ``covered_files`` raw-digest authorisation, an mtime
older than the configured cutoff, and a still-current, non-active, non-control
identity.  During an outage nothing is tightened, vacuumed or pruned; the
journal's native hard caps remain the finite local bound.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
import time
import urllib.error
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import AbstractSet, Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

from proxmox_fleet import http as _http
from proxmox_fleet import housekeeping_io as _io
from proxmox_fleet import housekeeping_native as native
from proxmox_fleet.alloy import DesiredAlloyConfig, parse_probe
from proxmox_fleet.executor import Executor
from proxmox_fleet.housekeeping import (
    HousekeepingProbe,
    base_uses_journal_env,
    guest_write_endpoint,
    journal_env_content,
    probe_housekeeping,
)
from proxmox_fleet.housekeeping_checkpoint import (
    CheckpointStore,
    DeliveryVerification,
    GuestKey,
    InitialManifestState,
    PruneIntent,
)
from proxmox_fleet.housekeeping_io import (
    GUEST_LOG_ROOTS,
    NPM_LOG_ROOT,
    PBS_API_ROOT,
    QUARANTINE_DIRNAME,
)
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult

__all__ = [
    "RetentionResult",
    "apply_guest_retention",
    "plan_prune_candidates",
    "quarantine_path_for",
    "filter_policy_hashes",
    "verification_reused",
]

# --------------------------------------------------------------------------- #
# Fixed locations and bounds
# --------------------------------------------------------------------------- #

#: NPM log directory and the disposable-verification marker prefix.
NPM_LOG_DIR = NPM_LOG_ROOT
NPM_MARKER_PREFIX = "fleet-housekeeping-"

LOKI_PUSH_PATH = "/loki/api/v1/push"
LOKI_READY_PATH = "/ready"
LOKI_QUERY_PATH = "/loki/api/v1/query_range"

#: PBS proxy HTTPS endpoint (localhost, read-only probe).
PBS_API_PORT = 8007
PBS_API_ACCESS_LOG = f"{PBS_API_ROOT}/access.log"

#: Marker polling: bounded attempts with injectable delay.
VERIFY_ATTEMPTS = 6
VERIFY_DELAY = 10.0
#: How far before marker emission the Loki query window opens.
VERIFY_LOOKBACK_NS = 300 * 1_000_000_000

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

_SUPPORTED_OS: frozenset = frozenset({"debian", "ubuntu"})

_VALID_COMPRESSION = frozenset({"plain", "gzip", "zstd"})
_VALID_PROFILE = frozenset({"npm", "pbs"})
_VALID_LOG_KIND = frozenset({"application", "task", "api", "task_index"})


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class RetentionResult:
    """Outcome of the retention/cleanup slice for one guest."""

    changed: bool = False
    bytes_reclaimed: int = 0
    files_pruned: int = 0
    warnings: List[str] = field(default_factory=list)
    failed: bool = False
    findings: bool = False


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _bounded(text: Any, limit: int = 240) -> str:
    return " ".join(str(text).split())[:limit]


def _detail(result: PrimitiveResult) -> str:
    raw = result.stderr or result.stdout or "primitive failed"
    return _bounded(raw, 300)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedup(items: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _endpoint_key(url: str) -> Tuple[str, str, int, str]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, (parts.hostname or "").lower(), port, parts.path.rstrip("/")


def _log_root_for(path: str) -> Optional[str]:
    best: Optional[str] = None
    for root in GUEST_LOG_ROOTS:
        if path == root or path.startswith(root + "/"):
            if best is None or len(root) > len(best):
                best = root
    return best




def quarantine_path_for(path: str) -> str:
    """Exact same-filesystem quarantine path for one allowlisted log file."""
    root = _log_root_for(path)
    if root is None:
        raise ValueError(f"path is outside the allowlisted guest log roots: {path!r}")
    relative = path[len(root) :].lstrip("/")
    return f"{root}/{QUARANTINE_DIRNAME}/{relative}"


def is_protected_log_name(path: str, log_kind: str) -> Optional[str]:
    """Reason a file must never be pruned, or ``None`` for closed archives."""
    name = posixpath.basename(path)
    if log_kind == "task_index":
        return "PBS task index/control file"
    if log_kind == "application":
        if _io.NPM_ARCHIVE_RE.match(name):
            return None
        return "current NPM application log"
    if log_kind == "task":
        if name.startswith("UPID:"):
            return None
        return "PBS task control file"
    if log_kind == "api":
        if _io.PBS_API_ARCHIVE_RE.match(name):
            return None
        return "current PBS API log"
    return "unrecognised log kind"


def filter_policy_hashes(hashes: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Drop absent-file empty hashes so verification comparison stays stable."""
    out: Dict[str, str] = {}
    for name, value in (hashes or {}).items():
        if isinstance(value, str) and value:
            out[str(name)] = value
    return out


def verification_reused(
    stored: Optional[DeliveryVerification],
    *,
    effective_alloy_sha256: Optional[str],
    policy_hashes: Dict[str, str],
    recognized: Set[str],
) -> bool:
    """True when stored markers still cover the current effective hashes."""
    if stored is None:
        return False
    if stored.effective_alloy_sha256 != effective_alloy_sha256:
        return False
    if filter_policy_hashes(stored.policy_hashes) != filter_policy_hashes(policy_hashes):
        return False
    if stored.journal_marker_ns is None:
        return False
    if recognized and stored.file_marker_ns is None:
        return False
    return True


# --------------------------------------------------------------------------- #
# Guest-side bounded actions
# --------------------------------------------------------------------------- #


_NATIVE_LOG_PROGRAM = (
    "import hashlib, json, os, stat, sys\n"
    "path, mode = sys.argv[1:3]\n"
    "fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)\n"
    "try:\n"
    "    metadata = os.fstat(fd)\n"
    "    if not stat.S_ISREG(metadata.st_mode): raise ValueError('log is not regular')\n"
    "    limit = 65536 if mode == 'presence' else 8192\n"
    "    start = max(0, metadata.st_size - limit)\n"
    "    raw = os.pread(fd, limit, start)\n"
    "finally:\n"
    "    os.close(fd)\n"
    "if mode == 'presence':\n"
    "    result = {'path_token': sys.argv[3].encode() in raw, 'ua_token': sys.argv[4].encode() in raw}\n"
    "else:\n"
    "    lines = raw.split(b'\\n')[:-1]\n"
    "    if start: lines = lines[1:]\n"
    "    line = next((line for line in reversed(lines) if line), b'')\n"
    "    line.decode('utf-8')\n"
    "    result = {'sha256': hashlib.sha256(line).hexdigest() if line else '', 'bytes': len(line)}\n"
    "sys.stdout.write(json.dumps(result, separators=(',', ':')))\n"
)
_PBS_PROBE_PROGRAM = (
    "import ssl, sys, urllib.error, urllib.request\n"
    "ctx = ssl.create_default_context(cafile='/etc/proxmox-backup/proxy.pem')\n"
    "request = urllib.request.Request(sys.argv[1], headers={'User-Agent': sys.argv[2]})\n"
    "try:\n"
    "    with urllib.request.urlopen(request, context=ctx, timeout=10) as response:\n"
    "        response.read(65536)\n"
    "except urllib.error.HTTPError:\n"
    "    pass\n"
)


def native_log_command(path: str, mode: str, *tokens: str) -> str:
    arguments = " ".join(shlex.quote(value) for value in (path, mode, *tokens))
    return f"python3 -c {shlex.quote(_NATIVE_LOG_PROGRAM)} {arguments}"


def pbs_probe_tokens(marker: str) -> Tuple[str, str]:
    return f"fleet-hk-p-{marker}", f"fleet-hk-u-{marker}"


def pbs_probe_url(path_token: str) -> str:
    return f"https://127.0.0.1:{PBS_API_PORT}/api2/json/fleet-housekeeping-{path_token}"


def pbs_probe_command(path_token: str, user_agent: str) -> str:
    return (
        f"python3 -c {shlex.quote(_PBS_PROBE_PROGRAM)} "
        f"{shlex.quote(pbs_probe_url(path_token))} {shlex.quote(user_agent)}"
    )


def journal_marker_command(marker: str) -> str:
    return f"logger -t fleet-housekeeping -- {shlex.quote(marker)}"


def npm_marker_path(marker: str) -> str:
    return f"{NPM_LOG_DIR}/{NPM_MARKER_PREFIX}{marker}.log"


def npm_marker_commands(path: str, marker: str) -> List[str]:
    return [native.write_file_command(path, marker + "\n", kind="marker")]


def npm_marker_cleanup_command(path: str) -> str:
    return f"rm -f -- {shlex.quote(path)}"


def _apply(
    executor: Executor, lxc_id: str, command: str
) -> Tuple[Optional[str], PrimitiveResult]:
    try:
        result = executor.housekeeping_apply(lxc_id, command=command)
    except Exception as exc:  # noqa: BLE001 - transport boundary, fail closed
        return f"{type(exc).__name__}: {_bounded(str(exc), 200)}", PrimitiveResult(
            rc=1, failed=True, stderr=str(exc)
        )
    if result.failed or result.rc != 0:
        return _detail(result), result
    return None, result


def _run_commands(
    executor: Executor, lxc_id: str, commands: Sequence[str]
) -> Tuple[Optional[str], List[PrimitiveResult]]:
    results: List[PrimitiveResult] = []
    for command in commands:
        error, result = _apply(executor, lxc_id, command)
        results.append(result)
        if error is not None:
            return error, results
    return None, results


def _read_guest_file(
    executor: Executor, lxc_id: str, path: str
) -> Tuple[str, Optional[str], Optional[str]]:
    """Return ``(status, content, error)`` with status ok/missing/error."""
    error, result = _apply(executor, lxc_id, native.read_file_command(path))
    if error is not None:
        return "error", None, error
    try:
        envelope = json.loads(result.stdout)
    except (ValueError, TypeError):
        return "error", None, "invalid native policy read response"
    if isinstance(envelope, dict):
        if set(envelope) == {"missing"} and envelope["missing"] is True:
            return "missing", None, None
        if set(envelope) == {"content"} and isinstance(envelope["content"], str):
            return "ok", envelope["content"], None
    return "error", None, "invalid native policy read response"


def _read_native_log(executor: Executor, lxc_id: str, path: str, mode: str, *tokens: str) -> Dict[str, Any]:
    error, result = _apply(executor, lxc_id, native_log_command(path, mode, *tokens))
    if error is not None:
        return {}
    try:
        observed = json.loads(result.stdout)
    except (ValueError, TypeError):
        return {}
    return observed if isinstance(observed, dict) else {}


# --------------------------------------------------------------------------- #
# Loki readiness / query
# --------------------------------------------------------------------------- #


def loki_ready(base_url: str, *, timeout: float = 30.0) -> Tuple[bool, str]:
    """Return ``(ready, detail)`` from the Loki ``/ready`` endpoint."""
    url = base_url.rstrip("/") + LOKI_READY_PATH
    try:
        response = _http.request(url, method="GET", timeout=timeout)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - any transport failure is "not ready"
        return False, type(exc).__name__
    return response.status == 200, f"HTTP {response.status}"


def loki_query_url(
    base_url: str, query: str, start_ns: int, end_ns: int, *, limit: int = 100
) -> str:
    params = urllib.parse.urlencode(
        {
            "query": query,
            "start": str(int(start_ns)),
            "end": str(int(end_ns)),
            "limit": str(int(limit)),
            "direction": "backward",
        }
    )
    return base_url.rstrip("/") + LOKI_QUERY_PATH + "?" + params


def _logql_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def journal_marker_query(marker: str) -> str:
    return '{job="systemd-journal",role="guest"} |= ' + _logql_string(marker)


def file_marker_query(labels: Dict[str, str], filename: str, token: str) -> str:
    parts = [f"{key}={_logql_string(str(value))}" for key, value in sorted(labels.items())]
    return (
        "{" + ",".join(parts) + "} | unpack | filename=" + _logql_string(filename)
        + (" |= " + _logql_string(token) if token else "")
    )


def payload_has_marker(
    payload: Any, token: str, *, filename: Optional[str], require_delivery: bool
) -> bool:
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    result = data.get("result")
    if not isinstance(result, list):
        return False
    for stream in result:
        if not isinstance(stream, dict):
            continue
        labels = stream.get("stream")
        if not isinstance(labels, dict):
            continue
        if require_delivery and labels.get("delivery") != "live":
            continue
        if filename is not None and labels.get("filename") != filename:
            continue
        values = stream.get("values")
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, list) and len(value) >= 2 and token in str(value[1]):
                return True
    return False


def _query_marker(
    base_url: str,
    query: str,
    start_ns: int,
    end_ns: int,
    *,
    token: str,
    filename: Optional[str],
    require_delivery: bool,
    timeout: float = 30.0,
) -> bool:
    url = loki_query_url(base_url, query, start_ns, end_ns)
    try:
        payload = _http.get_json(url, timeout=timeout)
    except Exception:  # noqa: BLE001 - a failed query is simply "not confirmed"
        return False
    return payload_has_marker(
        payload, token, filename=filename, require_delivery=require_delivery
    )


# --------------------------------------------------------------------------- #
# Prune planning
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Wire:
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


def _validated_wire(raw: Any) -> Optional[_Wire]:
    if not isinstance(raw, dict):
        return None
    keys = (
        "path",
        "device",
        "inode",
        "size",
        "mtime_ns",
        "allocated_bytes",
        "compression",
        "profile",
        "log_kind",
    )
    for key in keys[:1]:
        if not isinstance(raw.get(key), str) or not raw[key].startswith("/"):
            return None
    for key in ("device", "inode", "size", "mtime_ns", "allocated_bytes"):
        if not _is_int(raw.get(key)) or raw[key] < 0:
            return None
    if not isinstance(raw.get("is_active"), bool):
        return None
    if raw.get("compression") not in _VALID_COMPRESSION:
        return None
    if raw.get("profile") not in _VALID_PROFILE:
        return None
    if raw.get("log_kind") not in _VALID_LOG_KIND:
        return None
    return _Wire(
        path=raw["path"],
        device=raw["device"],
        inode=raw["inode"],
        size=raw["size"],
        mtime_ns=raw["mtime_ns"],
        allocated_bytes=raw["allocated_bytes"],
        compression=raw["compression"],
        profile=raw["profile"],
        log_kind=raw["log_kind"],
        is_active=raw["is_active"],
    )


def _wire_spec(wire: _Wire) -> Dict[str, Any]:
    return {
        "path": wire.path,
        "device": wire.device,
        "inode": wire.inode,
        "size": wire.size,
        "mtime_ns": wire.mtime_ns,
        "allocated_bytes": wire.allocated_bytes,
        "compression": wire.compression,
        "profile": wire.profile,
        "log_kind": wire.log_kind,
        "is_active": wire.is_active,
    }


def _identity(wire: _Wire) -> Tuple[int, int, int, int]:
    return (wire.device, wire.inode, wire.size, wire.mtime_ns)


#: Exact identity of a deletion this policy owns: (path, device, inode, size,
#: mtime_ns, acknowledged digest).  A covered source that vanished under an
#: owned identity is the expected result of our own quarantine, never a finding.
_OwnedDeletion = Tuple[str, int, int, int, int, str]


def _wire_owned_deletion(wire: _Wire, digest: str) -> _OwnedDeletion:
    return (wire.path, wire.device, wire.inode, wire.size, wire.mtime_ns, digest)


def _spec_owned_deletion(spec: Dict[str, Any]) -> _OwnedDeletion:
    return (
        str(spec["path"]),
        int(spec["device"]),
        int(spec["inode"]),
        int(spec["size"]),
        int(spec["mtime_ns"]),
        str(spec["sha256"]),
    )


def _intent_owned_deletion(intent: PruneIntent) -> Optional[_OwnedDeletion]:
    digest = intent.digest
    if not isinstance(digest, str) or not _SHA256_RE.match(digest):
        return None
    return (intent.path, intent.device, intent.inode, intent.size, intent.mtime_ns, digest)


def plan_prune_candidates(
    covered_files: Sequence[Dict[str, Any]],
    *,
    probe_files: Sequence[Dict[str, Any]],
    recognized: Set[str],
    retention_hours: int,
    now_ns: int,
    owned_deletions: AbstractSet[_OwnedDeletion] = frozenset(),
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Authorize deletions from the importer's acknowledged ``covered_files``.

    The candidate digest comes only from the importer; every candidate must
    still be closed, older than the cutoff, non-active and identity-unchanged in
    the current probe.  Returns ``(specs, warnings)``.

    ``owned_deletions`` carries the exact identities this policy already
    resolved by an observed helper outcome (or persisted as ``done`` intents).
    A covered source absent under one of those identities is the expected result
    of our own deletion and is skipped silently; a source missing for any other
    reason stays a conservative finding.
    """
    warnings: List[str] = []
    candidates: List[Dict[str, Any]] = []
    probe_by_path: Dict[str, _Wire] = {}
    for item in probe_files:
        wire = _validated_wire(item)
        if wire is not None:
            probe_by_path[wire.path] = wire
    cutoff_ns = int(now_ns) - int(retention_hours) * 3600 * 1_000_000_000
    for raw in covered_files:
        wire = _validated_wire(raw)
        digest = raw.get("sha256") if isinstance(raw, dict) else None
        path = raw.get("path") if isinstance(raw, dict) else None
        if wire is None:
            warnings.append("covered file entry is malformed; not pruning")
            continue
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            warnings.append(
                f"covered file {wire.path} has no valid acknowledged digest; not pruning"
            )
            continue
        if wire.profile not in recognized:
            warnings.append(
                f"covered file {wire.path} belongs to an unrecognized profile; not pruning"
            )
            continue
        if wire.log_kind == "task_index":
            continue
        if is_protected_log_name(wire.path, wire.log_kind) is not None:
            continue
        if wire.is_active:
            continue
        if wire.mtime_ns >= cutoff_ns:
            continue
        current = probe_by_path.get(wire.path)
        if current is None:
            if _wire_owned_deletion(wire, digest) in owned_deletions:
                continue
            warnings.append(
                f"covered file {wire.path} is absent from the current probe; not pruning"
            )
            continue
        if _identity(current) != _identity(wire):
            warnings.append(
                f"covered file {path} changed identity since acknowledgement; not pruning"
            )
            continue
        if current.is_active:
            continue
        spec = _wire_spec(wire)
        spec["sha256"] = digest
        source_id = raw.get("source_id") if isinstance(raw, dict) else None
        if isinstance(source_id, str) and source_id:
            spec["source_id"] = source_id
        try:
            spec["quarantine_path"] = quarantine_path_for(wire.path)
        except ValueError as exc:
            warnings.append(f"covered file {wire.path} is outside the allowlist: {exc}")
            continue
        candidates.append(spec)
    return candidates, warnings


def _prune_intent_id(key: GuestKey, spec: Dict[str, Any]) -> str:
    seed = f"{key.cluster}\0{key.node}\0{key.lxc_id}\0{spec['path']}\0{spec['device']}\0{spec['inode']}"
    return _sha256_text(seed)[:32]


def _recovery_spec(
    store: CheckpointStore,
    key: GuestKey,
    intent: PruneIntent,
    warnings: List[str],
) -> Optional[Tuple[PruneIntent, Dict[str, Any]]]:
    record = store.source(key, intent.source_id) if intent.source_id else None
    if (
        record is None or not record.acknowledged or record.digest != intent.digest
        or (record.provenance or {}).get("complete_size") != intent.size
        or (record.device, record.inode, record.size, record.mtime_ns) != (
            intent.device, intent.inode, intent.size, intent.mtime_ns
        )
        or record.is_active or is_protected_log_name(intent.path, record.log_kind) is not None
    ):
        warnings.append("unresolved prune intent lacks durable full-content coverage; retained")
        return None
    profile, log_kind = record.profile, record.log_kind
    compression, allocated, is_active = record.compression, record.allocated_bytes, False
    spec = {
        "path": intent.path,
        "device": intent.device,
        "inode": intent.inode,
        "size": intent.size,
        "mtime_ns": intent.mtime_ns,
        "allocated_bytes": allocated,
        "compression": compression,
        "profile": profile,
        "log_kind": log_kind,
        "is_active": is_active,
        "sha256": intent.digest,
        "quarantine_path": intent.quarantine_path,
    }
    return intent, spec


def _resolve_outcomes(
    store: CheckpointStore,
    batch: Sequence[Tuple[PruneIntent, Dict[str, Any]]],
    result: PrimitiveResult,
    warnings: List[str],
    owned_deletions: Set[_OwnedDeletion],
) -> Tuple[int, int, bool]:
    facts = result.facts if isinstance(result.facts, dict) else {}
    entries = facts.get("files")
    by_path: Dict[str, Dict[str, Any]] = {}
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                by_path.setdefault(entry["path"], entry)
    bytes_total = 0
    pruned = 0
    failed = False
    for intent, spec in batch:
        entry = by_path.get(spec["path"])
        if entry is None:
            warnings.append(
                f"prune outcome missing for {spec['path']}; intent retained for recovery"
            )
            failed = True
            continue
        state = str(entry.get("state") or "conflict")
        reclaimed = entry.get("bytes_reclaimed")
        reclaimed = reclaimed if isinstance(reclaimed, int) and not isinstance(reclaimed, bool) and reclaimed >= 0 else 0
        detail = _bounded(entry.get("detail") or "", 200)
        if state == "done":
            store.resolve_prune_intent(
                intent.intent_id, "done", reclaimed_bytes=reclaimed, detail=detail or None
            )
            # Only the helper's own per-file identity/digest/writer-checked
            # outcome authorizes this identity; never the transport's rc.
            owned_deletions.add(_spec_owned_deletion(spec))
            bytes_total += reclaimed
            pruned += 1
        elif state == "restored":
            store.resolve_prune_intent(
                intent.intent_id,
                "restored",
                reclaimed_bytes=0,
                detail=detail or "original path restored",
            )
            warnings.append(f"prune restored {spec['path']}: {detail or 'original path retained'}")
        else:
            store.resolve_prune_intent(
                intent.intent_id, "conflict", reclaimed_bytes=0, detail=detail or None
            )
            warnings.append(f"prune conflict for {spec['path']}: {detail or 'file retained'}")
    return bytes_total, pruned, failed


def _run_prune(
    executor: Executor,
    store: CheckpointStore,
    key: GuestKey,
    batch: Sequence[Tuple[PruneIntent, Dict[str, Any]]],
    warnings: List[str],
    owned_deletions: Set[_OwnedDeletion],
) -> Tuple[int, int, bool]:
    if not batch:
        return 0, 0, False
    specs = [spec for _intent, spec in batch]
    try:
        result = executor.housekeeping_prune(key.lxc_id, files=specs)
    except Exception as exc:  # noqa: BLE001 - transport boundary, fail closed
        warnings.append(
            "housekeeping prune transport failed; quarantine intents retained for recovery: "
            f"{type(exc).__name__}: {_bounded(str(exc), 200)}"
        )
        return 0, 0, True
    if result.failed or result.rc != 0:
        warnings.append(
            "housekeeping prune transport failed; quarantine intents retained for recovery: "
            + _detail(result)
        )
        return 0, 0, True
    return _resolve_outcomes(store, batch, result, warnings, owned_deletions)


def _record_prune_intents(
    store: CheckpointStore,
    key: GuestKey,
    candidates: Sequence[Dict[str, Any]],
) -> List[Tuple[PruneIntent, Dict[str, Any]]]:
    batch: List[Tuple[PruneIntent, Dict[str, Any]]] = []
    for spec in candidates:
        intent = PruneIntent(
            intent_id=_prune_intent_id(key, spec),
            key=key,
            path=spec["path"],
            quarantine_path=spec["quarantine_path"],
            device=spec["device"],
            inode=spec["inode"],
            size=spec["size"],
            mtime_ns=spec["mtime_ns"],
            source_id=spec.get("source_id"),
            digest=spec["sha256"],
        )
        store.record_prune_intent(intent)
        batch.append((intent, spec))
    return batch


# --------------------------------------------------------------------------- #
# NPM cutover
# --------------------------------------------------------------------------- #


def _marker_labels(key: GuestKey, name: str, *, app: str, log_kind: str) -> Dict[str, str]:
    """Live-delivery labels that match the per-guest Alloy file sources."""
    return {
        "job": "lxc-file",
        "delivery": "live",
        "cluster": key.cluster,
        "node": key.node,
        "guest_id": key.lxc_id,
        "host": name,
        "app": app,
        "log_kind": log_kind,
    }


@dataclass
class _VerificationOutcome:
    ok: bool
    journal_ok: bool
    file_ok: bool
    journal_ns: Optional[int]
    file_ns: Optional[int]
    warnings: List[str]


def _emit_and_query_markers(
    executor: Executor,
    settings: GlobalSettings,
    key: GuestKey,
    name: str,
    recognized: Set[str],
    base_url: str,
    *,
    retention_hours: int,
    probe_files: Sequence[Dict[str, Any]],
    sleep: Callable[[float], None],
    clock_ns: Callable[[], int],
    warnings: List[str],
) -> _VerificationOutcome:
    marker = uuid.uuid4().hex
    journal_token = f"fleet-hk-{marker}"
    local: List[str] = []

    error, _ = _apply(executor, key.lxc_id, journal_marker_command(journal_token))
    if error is not None:
        return _VerificationOutcome(
            False, False, False, None, None, [f"journal marker emission failed: {error}"]
        )

    file_markers: Dict[str, Tuple[str, str]] = {}
    disposable: Optional[str] = None
    for profile in sorted(recognized):
        if profile == "npm":
            path = npm_marker_path(marker)
            error, _ = _run_commands(executor, key.lxc_id, npm_marker_commands(path, marker))
            if error is not None:
                _apply(executor, key.lxc_id, npm_marker_cleanup_command(path))
                return _VerificationOutcome(
                    False,
                    True,
                    False,
                    None,
                    None,
                    [f"NPM file marker emission failed: {error}"],
                )
            disposable = path
            file_markers["npm"] = (marker, path)
        elif profile == "pbs":
            path_token, ua_token = pbs_probe_tokens(marker)
            error, _ = _apply(
                executor, key.lxc_id, pbs_probe_command(path_token, ua_token)
            )
            if error is not None:
                return _VerificationOutcome(
                    False,
                    True,
                    False,
                    None,
                    None,
                    [f"PBS API probe request failed: {error}"],
                )
            native: Dict[str, Any] = {}
            for attempt in range(VERIFY_ATTEMPTS):
                native = _read_native_log(
                    executor, key.lxc_id, PBS_API_ACCESS_LOG, "presence", path_token, ua_token
                )
                if native.get("path_token") is True or native.get("ua_token") is True:
                    break
                if attempt < VERIFY_ATTEMPTS - 1:
                    sleep(VERIFY_DELAY)
            if native.get("path_token") is True:
                query_token = path_token
            elif native.get("ua_token") is True:
                query_token = ua_token
                local.append("PBS access log verification uses its observed unique User-Agent token")
            else:
                return _VerificationOutcome(False, False, False, None, None, [
                    "the native PBS access log did not confirm the read-only API probe; deletion paused"
                ])
            file_markers["pbs"] = (query_token, PBS_API_ACCESS_LOG)

    emitted_ns = int(clock_ns())
    start_ns = emitted_ns - VERIFY_LOOKBACK_NS
    journal_ok = False
    file_ok = {profile: False for profile in file_markers}
    for attempt in range(VERIFY_ATTEMPTS):
        end_ns = int(clock_ns()) + 5 * 1_000_000_000
        if not journal_ok:
            journal_ok = _query_marker(
                base_url,
                journal_marker_query(journal_token),
                start_ns,
                end_ns,
                token=journal_token,
                filename=None,
                require_delivery=False,
            )
        for profile, (token, filename) in file_markers.items():
            if file_ok[profile]:
                continue
            labels = (
                _marker_labels(key, name, app="nginxproxymanager", log_kind="application")
                if profile == "npm"
                else _marker_labels(key, name, app="proxmox-backup", log_kind="api")
            )
            file_ok[profile] = _query_marker(
                base_url,
                file_marker_query(labels, filename, token),
                start_ns,
                end_ns,
                token=token,
                filename=filename,
                require_delivery=True,
            )
        if journal_ok and all(file_ok.values()):
            break
        if attempt < VERIFY_ATTEMPTS - 1:
            sleep(VERIFY_DELAY)

    if disposable is not None:
        error, _ = _apply(executor, key.lxc_id, npm_marker_cleanup_command(disposable))
        if error is not None:
            local.append(f"could not remove the disposable NPM marker file: {error}")

    if "pbs" in recognized and file_ok.get("pbs"):
        task_errors = _confirm_pbs_task_line(
            executor, key, name, probe_files, base_url, clock_ns,
            retention_hours=retention_hours, sleep=sleep,
        )
        local.extend(task_errors)
        file_ok["pbs"] = not task_errors

    ok = journal_ok and all(file_ok.values())
    return _VerificationOutcome(
        ok=ok,
        journal_ok=journal_ok,
        file_ok=all(file_ok.values()),
        journal_ns=emitted_ns if journal_ok else None,
        file_ns=emitted_ns if file_ok and all(file_ok.values()) else None,
        warnings=local,
    )


def _confirm_pbs_task_line(
    executor: Executor,
    key: GuestKey,
    name: str,
    probe_files: Sequence[Dict[str, Any]],
    base_url: str,
    clock_ns: Callable[[], int],
    *,
    retention_hours: int,
    sleep: Callable[[float], None],
) -> List[str]:
    """Compare native and live-entry digests without exposing task text to Runner."""
    candidates = [
        wire for item in probe_files
        for wire in [_validated_wire(item)] if wire is not None and wire.log_kind == "task"
    ]
    if not candidates:
        return ["no genuine PBS task log is available to verify live task delivery"]
    candidate = max(candidates, key=lambda wire: wire.mtime_ns)
    start_ns = int(clock_ns()) - int(retention_hours) * 3600 * 1_000_000_000
    if candidate.mtime_ns < start_ns:
        return ["no genuine PBS task log exists inside the live retention window"]
    observed = _read_native_log(executor, key.lxc_id, candidate.path, "line")
    digest = observed.get("sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        return ["a genuine PBS task line could not be fingerprinted"]
    labels = _marker_labels(key, name, app="proxmox-backup", log_kind="task")
    query = file_marker_query(labels, candidate.path, "")
    for attempt in range(VERIFY_ATTEMPTS):
        try:
            payload = _http.get_json(loki_query_url(
                base_url, query, start_ns, int(clock_ns()) + 5_000_000_000, limit=100
            ), timeout=30.0)
            streams = payload.get("data", {}).get("result", []) if isinstance(payload, dict) else []
            for stream in streams:
                if not isinstance(stream, dict):
                    continue
                stream_labels = stream.get("stream", {})
                if not isinstance(stream_labels, dict) or any(
                    stream_labels.get(label) != value for label, value in labels.items()
                ) or stream_labels.get("filename") != candidate.path:
                    continue
                for value in stream.get("values", []):
                    if isinstance(value, list) and len(value) == 2 and isinstance(value[1], str):
                        if hashlib.sha256(value[1].encode("utf-8")).hexdigest() == digest:
                            return []
        except (ValueError, TypeError, AttributeError, OSError):
            pass
        if attempt < VERIFY_ATTEMPTS - 1:
            sleep(VERIFY_DELAY)
    return ["genuine PBS task delivery is not queryable by packed filename; deletion paused"]


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #


def _manifest_gate(state: InitialManifestState) -> Optional[str]:
    if state.pending_batch:
        return "a pending Loki push batch is not acknowledged"
    if state.remaining > 0:
        return f"{state.remaining} initial-manifest prefix(es) remain unacknowledged"
    if state.total > 0 and state.capture_state != "complete":
        return f"initial capture state is {state.capture_state!r}"
    return None


@dataclass(frozen=True)
class _DeletionAuthorization:
    candidates: Tuple[Dict[str, Any], ...] = ()
    warnings: Tuple[str, ...] = ()
    blocked: Optional[str] = None


def _authorize_deletion(
    executor: Executor,
    store: CheckpointStore,
    key: GuestKey,
    settings: GlobalSettings,
    effective_alloy: DesiredAlloyConfig,
    probe: HousekeepingProbe,
    required_hashes: Dict[str, str],
    *,
    covered_files: Sequence[Dict[str, Any]] = (),
    owned_deletions: AbstractSet[_OwnedDeletion] = frozenset(),
    now_ns: int,
) -> _DeletionAuthorization:
    """One fail-closed policy decision; the helper still checks filesystem races."""
    if not probe.is_running or probe.is_template or probe.os_type not in _SUPPORTED_OS:
        return _DeletionAuthorization(blocked="guest is not an eligible running Debian guest")
    recognized = set(probe.recognized)
    if recognized and not probe.log_access_ready:
        return _DeletionAuthorization(blocked="scoped live-log permissions are not verified")
    if _endpoint_conflict(settings, effective_alloy) is not None:
        return _DeletionAuthorization(blocked="live delivery endpoint does not match")
    if any(probe.policy_sha256.get(key) != value for key, value in required_hashes.items()):
        return _DeletionAuthorization(blocked="observed owned policies do not match desired retention")
    try:
        manifest_error = _manifest_gate(store.initial_manifest_state(key))
        if manifest_error is not None:
            return _DeletionAuthorization(blocked=manifest_error)
        if not verification_reused(
            store.delivery_verification(key), effective_alloy_sha256=effective_alloy.sha256,
            policy_hashes=filter_policy_hashes(probe.policy_sha256), recognized=recognized,
        ):
            return _DeletionAuthorization(blocked="current configuration lacks matching delivery proof")
        candidates, warnings = plan_prune_candidates(
            covered_files, probe_files=probe.files, recognized=recognized,
            retention_hours=int(settings.housekeeping_local_retention_hours), now_ns=now_ns,
            owned_deletions=owned_deletions,
        )
        for candidate in candidates:
            source_id = candidate.get("source_id")
            record = store.source(key, source_id) if isinstance(source_id, str) else None
            if (
                record is None or not record.acknowledged
                or record.digest != candidate["sha256"]
                or (record.provenance or {}).get("complete_size") != candidate["size"]
                or (record.device, record.inode, record.size, record.mtime_ns) != (
                    candidate["device"], candidate["inode"], candidate["size"], candidate["mtime_ns"]
                )
            ):
                return _DeletionAuthorization(blocked="current file lacks durable full-content coverage")
    except Exception as exc:
        return _DeletionAuthorization(blocked=f"deletion checkpoint is unusable ({type(exc).__name__})")
    try:
        live = executor.alloy_probe(lxc_id=key.lxc_id)
        if not live.ok or parse_probe(live.facts).drift(effective_alloy.sha256):
            return _DeletionAuthorization(blocked="current Alloy service or configuration is not compliant")
    except Exception as exc:
        return _DeletionAuthorization(blocked=f"current Alloy health is unknown ({type(exc).__name__})")
    ready, detail = loki_ready(settings.require_loki_url())
    if not ready:
        return _DeletionAuthorization(blocked=f"live delivery is unavailable ({detail})")
    return _DeletionAuthorization(tuple(candidates), tuple(warnings))


def _endpoint_conflict(
    settings: GlobalSettings, effective_alloy: DesiredAlloyConfig
) -> Optional[str]:
    try:
        expected = settings.require_loki_url().rstrip("/") + LOKI_PUSH_PATH
    except Exception as exc:  # noqa: BLE001 - missing/invalid configured base
        return f"housekeeping Loki base is unusable: {_bounded(str(exc), 160)}"
    endpoint = guest_write_endpoint(effective_alloy.content)
    if endpoint is None:
        return "the desired guest Alloy config has no Loki write endpoint"
    if _endpoint_key(endpoint) != _endpoint_key(expected):
        return "the guest Alloy write endpoint does not match the configured housekeeping Loki base"
    return None


def _lingering_npm_writer(probe_files: Sequence[Dict[str, Any]]) -> Optional[str]:
    for item in probe_files:
        wire = _validated_wire(item)
        if wire is None or wire.profile != "npm" or wire.log_kind != "application":
            continue
        if not wire.is_active:
            continue
        if _io.NPM_ARCHIVE_RE.match(posixpath.basename(wire.path)):
            return wire.path
    return None


def apply_guest_retention(
    executor: Executor,
    settings: GlobalSettings,
    store: CheckpointStore,
    key: GuestKey,
    probe: HousekeepingProbe,
    effective_alloy: DesiredAlloyConfig,
    *,
    covered_files: Sequence[Dict[str, Any]],
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock_ns: Callable[[], int] = time.time_ns,
) -> RetentionResult:
    """Tighten local retention and prune acknowledged closed file logs.

    Returns explicit findings; nothing is tightened, vacuumed or pruned until
    the initial manifest is fully acknowledged, the endpoint is confirmed ready
    and a matching real delivery verification exists.  A dry run only reports
    what it would do and never emits markers, writes policy, vacuums, prunes or
    mutates the checkpoint.
    """
    warnings: List[str] = list(probe.warnings)
    if not probe.is_running or probe.is_template:
        return RetentionResult(
            warnings=_dedup(
                warnings + [f"guest {key.lxc_id} is stopped or a template; retention skipped"]
            )
        )

    recognized = set(probe.recognized)
    name = probe.name or key.lxc_id

    try:
        manifest = store.initial_manifest_state(key)
        stored = store.delivery_verification(key)
        open_intents = store.open_prune_intents(key)
        # Durable evidence of this policy's own completed deletions: a covered
        # source absent under one of these exact identities is expected, never
        # a finding.  Same-run successes are added as their outcomes are read.
        owned_deletions: Set[_OwnedDeletion] = set()
        for intent in store.prune_intents(key):
            identity = _intent_owned_deletion(intent) if intent.state == "done" else None
            if identity is not None:
                owned_deletions.add(identity)
    except Exception as exc:  # noqa: BLE001 - untrusted checkpoint blocks deletion
        return RetentionResult(
            warnings=_dedup(
                warnings + [f"housekeeping checkpoint is unusable: {_bounded(str(exc), 200)}"]
            ),
            failed=True,
        )

    endpoint_error = _endpoint_conflict(settings, effective_alloy)
    env_error = (
        None
        if base_uses_journal_env(effective_alloy)
        else (
            "the desired journal source does not use FLEET_JOURNAL_MAX_AGE; explicit "
            "migration is required before tightening retention"
        )
    )
    manifest_error = _manifest_gate(manifest)
    acl_error: Optional[str] = None
    if recognized:
        if not probe.log_access_ready:
            acl_error = (
                "Alloy lacks confirmed scoped read access to the managed log trees; "
                "explicit initial permission setup is required"
            )
        elif probe.os_type not in _SUPPORTED_OS:
            acl_error = f"guest OS {probe.os_type or 'unknown'!r} is not Debian/apt-based"

    base_url = settings.housekeeping_loki_url.rstrip("/") if settings.housekeeping_loki_url else ""

    if dry_run:
        return _audit(
            executor=executor,
            settings=settings,
            key=key,
            name=name,
            recognized=recognized,
            probe=probe,
            effective_alloy=effective_alloy,
            covered_files=covered_files,
            owned_deletions=owned_deletions,
            base_url=base_url,
            stored=stored,
            open_intents=open_intents,
            endpoint_error=endpoint_error,
            env_error=env_error,
            manifest_error=manifest_error,
            acl_error=acl_error,
            clock_ns=clock_ns,
            warnings=warnings,
        )

    for message in (endpoint_error, env_error, acl_error):
        if message is not None:
            return RetentionResult(warnings=_dedup(warnings + [message]), failed=True)
    if manifest_error is not None:
        return RetentionResult(
            warnings=_dedup(
                warnings
                + [f"{manifest_error}; retention tightening, journal vacuum and file pruning deferred"]
            )
        )
    if not base_url:
        return RetentionResult(
            warnings=_dedup(warnings + ["housekeeping Loki base is not configured"]),
            failed=True,
        )

    ready, ready_detail = loki_ready(base_url)
    if not ready:
        return RetentionResult(
            warnings=_dedup(
                warnings
                + [
                    f"Loki readiness check failed ({ready_detail}); journal vacuum, configuration "
                    "tightening and file pruning are paused — the journal's native size/time caps "
                    "remain the finite local bound"
                ]
            ),
            failed=True,
        )

    hashes = filter_policy_hashes(probe.policy_sha256)
    reused = verification_reused(
        stored,
        effective_alloy_sha256=effective_alloy.sha256,
        policy_hashes=hashes,
        recognized=recognized,
    )
    outcome: Optional[_VerificationOutcome] = None
    if not reused:
        outcome = _emit_and_query_markers(
            executor,
            settings,
            key,
            name,
            recognized,
            base_url,
            retention_hours=int(settings.housekeeping_local_retention_hours),
            probe_files=probe.files,
            sleep=sleep,
            clock_ns=clock_ns,
            warnings=warnings,
        )
        warnings = _dedup(warnings + list(outcome.warnings))
        if not outcome.ok:
            reasons = []
            if not outcome.journal_ok:
                reasons.append("the journal marker was not queryable")
            if not outcome.file_ok:
                reasons.append("the live file marker was not queryable")
            return RetentionResult(
                warnings=_dedup(
                    warnings
                    + [
                        "delivery verification failed ("
                        + "; ".join(reasons)
                        + "); journal vacuum, configuration tightening and file pruning are paused"
                    ]
                ),
                failed=True,
            )

    ready, ready_detail = loki_ready(base_url)
    if not ready:
        return RetentionResult(
            warnings=_dedup(warnings + [f"Loki delivery unavailable before retention changes ({ready_detail})"]),
            failed=True,
        )
    # -- journald retention ------------------------------------------------- #
    changed = False
    bytes_reclaimed = 0
    journald_content = native.journald_policy_content(settings)
    desired_journald = _sha256_text(journald_content)
    if probe.policy_sha256.get("journald", "") != desired_journald:
        error, _ = _run_commands(executor, key.lxc_id, native.journald_policy_commands(settings))
        if error is not None:
            return RetentionResult(
                changed=True,
                warnings=_dedup(warnings + [f"journald retention policy failed: {error}"]),
                failed=True,
            )
        changed = True


    # -- NPM hourly rotation cutover ---------------------------------------- #
    npm_managed_content: Optional[str] = None
    if "npm" in recognized:
        npm_changed, npm_managed_content, npm_error = _apply_npm_cutover(
            executor, key, probe
        )
        if npm_error is not None:
            return RetentionResult(
                changed=changed or npm_changed,
                bytes_reclaimed=bytes_reclaimed,
                warnings=_dedup(warnings + [npm_error]),
                failed=True,
            )
        if npm_changed:
            changed = True

    # Never infer observed policy/permissions from the commands we attempted.
    try:
        probe_for_prune = probe_housekeeping(executor, key.lxc_id)
    except Exception as exc:
        return RetentionResult(
            changed=changed, warnings=_dedup(warnings + [
                f"cannot verify current retention policy ({type(exc).__name__}); deletion paused"
            ]), failed=True,
        )
    current_hashes = filter_policy_hashes(probe_for_prune.policy_sha256)
    required_hashes = {
        "journald": desired_journald,
        "alloy_env": _sha256_text(journal_env_content(settings.housekeeping_local_retention_hours)),
    }
    if npm_managed_content is not None:
        required_hashes["npm_logrotate"] = _sha256_text(npm_managed_content)
    if any(probe_for_prune.policy_sha256.get(key) != value for key, value in required_hashes.items()):
        return RetentionResult(changed=changed, warnings=_dedup(warnings + [
            "owned retention policies were not observed after configuration; deletion paused"
        ]), failed=True)
    if outcome is None and current_hashes != hashes:
        outcome = _emit_and_query_markers(
            executor, settings, key, name, recognized, base_url,
            retention_hours=int(settings.housekeeping_local_retention_hours),
            probe_files=probe_for_prune.files, sleep=sleep, clock_ns=clock_ns,
            warnings=warnings,
        )
        warnings.extend(outcome.warnings)
        if not outcome.ok:
            return RetentionResult(changed=changed, warnings=_dedup(warnings + [
                "delivery verification failed after policy changes; deletion paused"
            ]), failed=True)
    if outcome is not None:
        persist_error = _persist_verification(store, key, effective_alloy, outcome, probe_for_prune)
        if persist_error is not None:
            return RetentionResult(changed=changed, warnings=_dedup(warnings + [persist_error]), failed=True)
    if not verification_reused(
        store.delivery_verification(key), effective_alloy_sha256=effective_alloy.sha256,
        policy_hashes=current_hashes, recognized=recognized,
    ) or (recognized and not probe_for_prune.log_access_ready):
        return RetentionResult(changed=changed, warnings=_dedup(warnings + [
            "current delivery verification or scoped permissions do not match; deletion paused"
        ]), failed=True)

    for command in native.journal_vacuum_commands(settings):
        authorization = _authorize_deletion(
            executor, store, key, settings, effective_alloy, probe_for_prune, required_hashes, now_ns=int(clock_ns())
        )
        if authorization.blocked is not None:
            return RetentionResult(changed=changed, warnings=_dedup(warnings + [
                authorization.blocked + "; journal vacuum paused; native caps remain finite"
            ]), failed=True)
        error, _ = _apply(executor, key.lxc_id, command)
        if error is not None:
            return RetentionResult(changed=changed, warnings=_dedup(warnings + [
                f"journal vacuum failed: {error}"
            ]), failed=True)
    try:
        after_vacuum = probe_housekeeping(executor, key.lxc_id)
    except Exception as exc:
        return RetentionResult(changed=changed, warnings=_dedup(warnings + [
            f"cannot measure journal reclamation ({type(exc).__name__}); file pruning paused"
        ]), failed=True)
    bytes_reclaimed += max(probe.journal_bytes - after_vacuum.journal_bytes, 0)
    changed |= bytes_reclaimed > 0
    if current_hashes != filter_policy_hashes(after_vacuum.policy_sha256) or (
        recognized and not after_vacuum.log_access_ready
    ):
        return RetentionResult(changed=changed, bytes_reclaimed=bytes_reclaimed,
            warnings=_dedup(warnings + ["policy or scoped permissions changed during vacuum; file pruning paused"]),
            failed=True)
    probe_for_prune = after_vacuum
    authorization = _authorize_deletion(
        executor, store, key, settings, effective_alloy, probe_for_prune, required_hashes,
        covered_files=covered_files, owned_deletions=owned_deletions, now_ns=int(clock_ns()),
    )
    if authorization.blocked is not None:
        return RetentionResult(changed=changed, bytes_reclaimed=bytes_reclaimed,
            warnings=_dedup(warnings + [authorization.blocked + "; file deletion paused"]), failed=True)

    # -- crash-resume quarantine recovery ----------------------------------- #
    # Pending intents from an interrupted run replay in the same bounded
    # 128-file batches as fresh candidates, with a fresh probe and a fresh
    # authorization per batch.  An unresolved intent or a failed batch leaves
    # every unconfirmed intent in place for the next run.
    new_warnings: List[str] = []
    recovered_files = 0
    recovered_bytes = 0
    for offset in range(0, len(open_intents), 128):
        chunk = open_intents[offset:offset + 128]
        recovery_batch: List[Tuple[PruneIntent, Dict[str, Any]]] = []
        for intent in chunk:
            spec_pair = _recovery_spec(store, key, intent, new_warnings)
            if spec_pair is None:
                return RetentionResult(
                    changed=changed or recovered_bytes > 0,
                    bytes_reclaimed=bytes_reclaimed + recovered_bytes,
                    files_pruned=recovered_files,
                    warnings=_dedup(warnings + new_warnings), failed=True,
                )
            recovery_batch.append(spec_pair)
        try:
            current_probe = probe_housekeeping(executor, key.lxc_id)
        except Exception as exc:
            return RetentionResult(
                changed=changed or recovered_bytes > 0,
                bytes_reclaimed=bytes_reclaimed + recovered_bytes,
                files_pruned=recovered_files,
                warnings=_dedup(warnings + new_warnings + [
                    f"cannot observe current recovery state ({type(exc).__name__}); intents retained"
                ]), failed=True,
            )
        recovery_gate = _authorize_deletion(
            executor, store, key, settings, effective_alloy, current_probe, required_hashes,
            owned_deletions=owned_deletions, now_ns=int(clock_ns()),
        )
        if recovery_gate.blocked is not None:
            return RetentionResult(
                changed=changed or recovered_bytes > 0,
                bytes_reclaimed=bytes_reclaimed + recovered_bytes,
                files_pruned=recovered_files,
                warnings=_dedup(warnings + new_warnings + [recovery_gate.blocked]), failed=True,
            )
        batch_bytes, batch_files, recovery_failed = _run_prune(
            executor, store, key, recovery_batch, new_warnings, owned_deletions
        )
        recovered_bytes += batch_bytes
        recovered_files += batch_files
        if recovery_failed:
            return RetentionResult(
                changed=changed or recovered_bytes > 0,
                bytes_reclaimed=bytes_reclaimed + recovered_bytes,
                files_pruned=recovered_files,
                warnings=_dedup(warnings + new_warnings), failed=True,
            )
    bytes_reclaimed += recovered_bytes

    # The pre-recovery probe still lists files this run just deleted; re-observe
    # so candidates reflect the post-recovery filesystem and the identities this
    # run resolved are recognized instead of reported missing.
    try:
        probe_for_prune = probe_housekeeping(executor, key.lxc_id)
    except Exception as exc:
        return RetentionResult(changed=changed, bytes_reclaimed=bytes_reclaimed,
            files_pruned=recovered_files, warnings=_dedup(warnings + new_warnings + [
                f"cannot observe current deletion state ({type(exc).__name__}); remaining files retained"
            ]), failed=True)
    authorization = _authorize_deletion(
        executor, store, key, settings, effective_alloy, probe_for_prune, required_hashes,
        covered_files=covered_files, owned_deletions=owned_deletions, now_ns=int(clock_ns()),
    )
    if authorization.blocked is not None:
        return RetentionResult(changed=changed, bytes_reclaimed=bytes_reclaimed,
            files_pruned=recovered_files,
            warnings=_dedup(warnings + new_warnings + [authorization.blocked]), failed=True)
    candidates = list(authorization.candidates)
    new_warnings.extend(authorization.warnings)
    pruned_files = 0
    for offset in range(0, len(candidates), 128):
        try:
            current_probe = probe_housekeeping(executor, key.lxc_id)
        except Exception as exc:
            return RetentionResult(changed=changed, bytes_reclaimed=bytes_reclaimed,
                files_pruned=pruned_files + recovered_files, warnings=_dedup(warnings + [
                    f"cannot observe current deletion state ({type(exc).__name__}); remaining files retained"
                ]), failed=True)
        batch_gate = _authorize_deletion(
            executor, store, key, settings, effective_alloy, current_probe, required_hashes,
            covered_files=candidates[offset:offset + 128], owned_deletions=owned_deletions,
            now_ns=int(clock_ns()),
        )
        new_warnings.extend(batch_gate.warnings)
        if batch_gate.blocked is not None:
            return RetentionResult(changed=changed, bytes_reclaimed=bytes_reclaimed,
                files_pruned=pruned_files + recovered_files,
                warnings=_dedup(warnings + new_warnings + [batch_gate.blocked]), failed=True)
        batch = _record_prune_intents(store, key, batch_gate.candidates)
        batch_bytes, batch_files, prune_failed = _run_prune(
            executor, store, key, batch, new_warnings, owned_deletions
        )
        bytes_reclaimed += batch_bytes
        pruned_files += batch_files
        changed |= batch_bytes > 0 or batch_files > 0
        if prune_failed:
            return RetentionResult(changed=changed, bytes_reclaimed=bytes_reclaimed,
                files_pruned=pruned_files + recovered_files,
                warnings=_dedup(warnings + new_warnings), failed=True)

    pruned_files += recovered_files
    return RetentionResult(
        changed=changed or bytes_reclaimed > 0 or pruned_files > 0,
        bytes_reclaimed=bytes_reclaimed,
        files_pruned=pruned_files,
        warnings=_dedup(warnings + new_warnings),
        failed=False,
    )


def _persist_verification(
    store: CheckpointStore,
    key: GuestKey,
    effective_alloy: DesiredAlloyConfig,
    outcome: _VerificationOutcome,
    probe: HousekeepingProbe,
) -> Optional[str]:
    """Persist successful markers only against observed owned-policy hashes."""
    try:
        store.record_delivery_verification(
            key, effective_alloy_sha256=effective_alloy.sha256,
            policy_hashes=filter_policy_hashes(probe.policy_sha256),
            journal_marker_ns=outcome.journal_ns, file_marker_ns=outcome.file_ns,
        )
    except Exception as exc:
        return f"could not persist delivery verification ({type(exc).__name__})"
    return None


def _reopen_rotated_npm_writers(
    executor: Executor, key: GuestKey, probe: Optional[HousekeepingProbe] = None,
) -> Tuple[bool, Optional[str]]:
    """Repair only observed stale writers; verify their fds actually moved."""
    try:
        before = probe if probe is not None else probe_housekeeping(executor, key.lxc_id)
        if not before.is_running or before.is_template:
            return False, "NPM writer state is unavailable for a stopped guest or template"
        if _lingering_npm_writer(before.files) is None:
            return False, None
        error, _ = _apply(executor, key.lxc_id, native.npm_reopen_command())
        if error is not None:
            return False, f"OpenResty log reopen failed: {error}"
        after = probe_housekeeping(executor, key.lxc_id)
        if not after.is_running or after.is_template:
            return False, "NPM guest state changed during log reopen"
        lingering = _lingering_npm_writer(after.files)
        if lingering is not None:
            return False, f"OpenResty still has a rotated writable log ({lingering}); rotation paused"
        return True, None
    except Exception as exc:
        return False, f"NPM writer state is unknown ({type(exc).__name__}); rotation paused"


def _apply_npm_cutover(
    executor: Executor,
    key: GuestKey,
    probe: HousekeepingProbe,
) -> Tuple[bool, Optional[str], Optional[str]]:
    status, original, error = _read_guest_file(executor, key.lxc_id, native.NPM_LOGROTATE_ORIGINAL)
    if status == "error":
        return False, None, f"could not read {native.NPM_LOGROTATE_ORIGINAL}: {error}"
    status_target, current_managed, error = _read_guest_file(
        executor, key.lxc_id, native.NPM_LOGROTATE_MANAGED
    )
    if status_target == "error":
        return False, None, f"could not read {native.NPM_LOGROTATE_MANAGED}: {error}"

    original_text = original or ""
    parsed_native = native.parse_logrotate(original_text) if status == "ok" and original_text else None
    if parsed_native is not None and parsed_native.ambiguous is not None:
        return False, None, f"cannot safely parse native NPM rotation policy: {parsed_native.ambiguous}"
    need_cutover = parsed_native is not None and bool(parsed_native.npm_blocks)
    remaining: Optional[str] = None
    if need_cutover:
        assert parsed_native is not None
        blocks = parsed_native.npm_blocks
        remaining = native.npm_remaining_content(original_text, blocks)
    elif status_target == "ok" and current_managed:
        parsed = native.parse_logrotate(current_managed)
        if parsed.ambiguous is not None or not parsed.npm_blocks:
            return False, None, "managed NPM policy cannot be reconciled safely"
        blocks = parsed.npm_blocks
    else:
        return False, None, "native NPM rotation policy is missing; writer mechanisms cannot be guessed"
    managed_content = native.build_npm_managed_logrotate(blocks)
    managed_differs = status_target != "ok" or current_managed != managed_content

    changed = False
    if need_cutover or managed_differs:
        error, _ = _run_commands(
            executor,
            key.lxc_id,
            native.npm_cutover_commands(
                managed_content=managed_content, remaining_content=remaining, original_content=original_text
            ),
        )
        if error is not None:
            return changed, managed_content, f"NPM logrotate cutover failed: {error}"
        changed = True

    reopened, error = _reopen_rotated_npm_writers(executor, key, probe)
    changed |= reopened
    if error is not None:
        return changed, managed_content, error

    error, _ = _apply(executor, key.lxc_id, native.npm_logrotate_command())
    if error is not None:
        return changed, managed_content, f"NPM logrotate run failed: {error}"
    reopened, error = _reopen_rotated_npm_writers(executor, key)
    return changed or reopened, managed_content, error


def _audit(
    *,
    executor: Executor,
    settings: GlobalSettings,
    key: GuestKey,
    name: str,
    recognized: Set[str],
    probe: HousekeepingProbe,
    effective_alloy: DesiredAlloyConfig,
    covered_files: Sequence[Dict[str, Any]],
    owned_deletions: AbstractSet[_OwnedDeletion],
    base_url: str,
    stored: Optional[DeliveryVerification],
    open_intents: Sequence[PruneIntent],
    endpoint_error: Optional[str],
    env_error: Optional[str],
    manifest_error: Optional[str],
    acl_error: Optional[str],
    clock_ns: Callable[[], int],
    warnings: List[str],
) -> RetentionResult:
    """Read-only audit: report findings without any guest or checkpoint write."""
    findings: List[str] = []
    for message in (endpoint_error, env_error, acl_error):
        if message is not None:
            findings.append(message)
    if manifest_error is not None:
        findings.append(f"{manifest_error}; retention and pruning would be deferred")

    verified = verification_reused(
        stored,
        effective_alloy_sha256=effective_alloy.sha256,
        policy_hashes=filter_policy_hashes(probe.policy_sha256),
        recognized=recognized,
    )
    ready = False
    if base_url:
        ready, ready_detail = loki_ready(base_url)
        if not ready:
            findings.append(f"Loki readiness check failed ({ready_detail})")
    else:
        findings.append("housekeeping Loki base is not configured")
    if not verified:
        findings.append("delivery verification would be re-run before any tightening or pruning")

    desired_journald = _sha256_text(native.journald_policy_content(settings))
    if probe.policy_sha256.get("journald", "") != desired_journald:
        findings.append("the journald retention drop-in would be written and journald restarted")
    if "npm" in recognized:
        status, original, _error = _read_guest_file(
            executor, key.lxc_id, native.NPM_LOGROTATE_ORIGINAL
        )
        status_target, current_managed, _error2 = _read_guest_file(
            executor, key.lxc_id, native.NPM_LOGROTATE_MANAGED
        )
        if status == "ok" and original:
            parsed = native.parse_logrotate(original)
            if parsed.ambiguous is not None:
                findings.append(f"NPM logrotate stanza is ambiguous: {parsed.ambiguous}")
            elif parsed.npm_blocks:
                findings.append(
                    "the NPM logrotate stanza would move to the managed hourly policy"
                )
        if status_target != "ok" or not current_managed:
            findings.append("the managed NPM hourly rotation policy requires its native writer mechanisms")
        else:
            parsed_managed = native.parse_logrotate(current_managed)
            if parsed_managed.ambiguous or native.build_npm_managed_logrotate(parsed_managed.npm_blocks) != current_managed:
                findings.append("managed NPM rotation directives require safe reconciliation")
        if _lingering_npm_writer(probe.files) is not None:
            findings.append("a rotated NPM writable log requires verified service-based reopen before rotation")

    if manifest_error is None and verified and ready and acl_error is None:
        candidates, _warnings = plan_prune_candidates(
            covered_files,
            probe_files=probe.files,
            recognized=recognized,
            retention_hours=int(settings.housekeeping_local_retention_hours),
            now_ns=int(clock_ns()),
            owned_deletions=owned_deletions,
        )
        for intent in open_intents:
            findings.append(f"an unfinished quarantine intent exists for {intent.path}")
        if candidates:
            reclaimed = sum(int(spec.get("allocated_bytes") or 0) for spec in candidates)
            findings.append(
                f"{len(candidates)} acknowledged closed file(s) would be pruned "
                f"(~{reclaimed} allocated bytes)"
            )

    if not findings:
        return RetentionResult(warnings=_dedup(warnings))
    return RetentionResult(
        warnings=_dedup(warnings + findings),
        findings=True,
    )
