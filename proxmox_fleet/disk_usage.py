"""Bounded, read-only root-filesystem directory diagnostics for disk warnings.

The node helper walks the guest root filesystem metadata-only and returns the
raw snapshot described in :class:`DiskUsageFacts`.  This module is the manager
side of that transport: a strict immutable wire parser, the disjoint ratio
filter that turns measured directories into at most twenty culprits, and the
single collector used by the existing low-space warning path.

Everything here is diagnostic: no fact is a cleanup entitlement, shares are
always shares of *used* root-filesystem bytes (never capacity), and any
malformed, failed or timed-out measurement degrades to an ``unavailable``
insight without raising into the update flow.
"""

from __future__ import annotations

import posixpath
import threading
import unicodedata
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    cast,
)

from proxmox_fleet.log_plugins import disk_usage_extra_paths
from proxmox_fleet.lxc_parse import parse_df_used_kb
from proxmox_fleet.models.log_plugins import DiskUsagePolicy, path_depth
from proxmox_fleet.models.state import DiskUsageEntry, DiskUsageInsight

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from proxmox_fleet.executor import Executor

# Wire/transport bounds.  The helper enforces the same hard limits; exceeding
# them on the manager side means the transport is malformed, never an excuse
# to trust a truncated snapshot as complete.
_MAX_WIRE_ROWS = 1000
_MAX_EXTRA_PATHS = 32
_MAX_EXTRA_DEPTH = 16
_REPORT_DEPTH = 3
_MAX_ENTRIES = 20
_MAX_DETAIL = 160
_MAX_INT = 2**63 - 1

_UNSAFE_TEXT_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_DEFAULT_CAP = 20.0
_DEFAULT_FLOOR_MB = 256

# One manager-wide budget for concurrent disk collectors (read-only I/O).
_COLLECTOR_SEMAPHORE = threading.Semaphore(2)


class _DiskFactsError(ValueError):
    """Internal strict-validation failure; never escapes the public helpers."""


class _InconsistentMeasurements(ValueError):
    """Measured bytes cannot be reconciled; proportions must stay unavailable."""


# --------------------------------------------------------------------------- #
# Typed wire facts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DiskUsageDirectory:
    """One measured directory aggregate from the helper snapshot."""

    path: str
    depth: int
    allocated_bytes: int
    direct_bytes: int


@dataclass(frozen=True)
class DiskUsageFacts:
    """Immutable, validated helper snapshot (raw measurements are retained)."""

    status: Literal["complete", "partial", "unavailable"]
    root_used_bytes: Optional[int]
    root_device: Optional[int]
    directories: Tuple[DiskUsageDirectory, ...]
    visited_entries: int
    excluded_mounts: int
    error_count: int
    reason: str
    extra_paths: Tuple[str, ...] = ()


def _unavailable_facts(reason: str) -> DiskUsageFacts:
    return DiskUsageFacts(
        status="unavailable",
        root_used_bytes=None,
        root_device=None,
        directories=(),
        visited_entries=0,
        excluded_mounts=0,
        error_count=0,
        reason=_bounded_detail(reason, "disk measurement unavailable"),
        extra_paths=(),
    )


def _bounded_detail(value: object, fallback: str) -> str:
    """Sanitize untrusted text into the model's bounded detail contract."""
    if not isinstance(value, str):
        return fallback
    cleaned = "".join(
        " " if unicodedata.category(char) in _UNSAFE_TEXT_CATEGORIES else char
        for char in value
    )
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return fallback
    return cleaned[: _MAX_DETAIL - 3] + "..." if len(cleaned) > _MAX_DETAIL else cleaned


def _normalize_directory(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _DiskFactsError(f"{field} must be a nonempty path")
    if any(unicodedata.category(char) in _UNSAFE_TEXT_CATEGORIES for char in value):
        raise _DiskFactsError(f"{field} contains control characters")
    if "://" in value or value.startswith("file:"):
        raise _DiskFactsError(f"{field} must be a filesystem path, not a URI")
    if any(char in "*?[" for char in value):
        raise _DiskFactsError(f"{field} must not contain glob characters")
    if "//" in value:
        raise _DiskFactsError(f"{field} must not contain empty path components")
    if not value.startswith("/"):
        raise _DiskFactsError(f"{field} must be an absolute path")
    if value != "/" and ".." in value.split("/"):
        raise _DiskFactsError(f"{field} must not contain '..'")
    if posixpath.normpath(value) != value:
        raise _DiskFactsError(f"{field} must be a normalized absolute path")
    return value


def _normalize_extra_paths(values: object) -> Tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or isinstance(values, Mapping):
        raise _DiskFactsError("extra_paths must be a sequence of paths")
    try:
        items = list(cast(Sequence[object], values))
    except TypeError as exc:
        raise _DiskFactsError("extra_paths must be a sequence of paths") from exc
    if len(items) > _MAX_EXTRA_PATHS:
        raise _DiskFactsError(
            f"extra_paths must contain at most {_MAX_EXTRA_PATHS} paths"
        )
    seen: Set[str] = set()
    normalized: List[str] = []
    for item in items:
        path = _normalize_directory(item, field="extra_paths entry")
        if path == "/":
            raise _DiskFactsError("extra_paths must not include the filesystem root")
        depth = path_depth(path)
        if depth > _MAX_EXTRA_DEPTH:
            raise _DiskFactsError(
                f"extra_paths depth must be at most {_MAX_EXTRA_DEPTH} ({path!r})"
            )
        if len(path.encode("utf-8")) > 512:
            raise _DiskFactsError("extra_paths entry must be at most 512 UTF-8 bytes")
        if path in seen:
            raise _DiskFactsError(f"duplicate extra_paths entry {path!r}")
        seen.add(path)
        normalized.append(path)
    return tuple(sorted(normalized))


def _count(value: object, field: str, *, allow_none: bool = False) -> Optional[int]:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _DiskFactsError(f"{field} must be a nonnegative integer")
    if value < 0:
        raise _DiskFactsError(f"{field} must be nonnegative")
    if value > _MAX_INT:
        raise _DiskFactsError(f"{field} is out of bounds")
    return value


def _parse_directory(
    row: object, *, extra_paths: Set[str]
) -> DiskUsageDirectory:
    if not isinstance(row, Mapping):
        raise _DiskFactsError("directories rows must be mappings")
    try:
        path_value = row["path"]
        depth_value = row["depth"]
        allocated_value = row["allocated_bytes"]
        direct_value = row["direct_bytes"]
    except KeyError as exc:
        raise _DiskFactsError("directories rows are missing required fields") from exc
    path = _normalize_directory(path_value, field="directory path")
    depth = _count(depth_value, "directory depth")
    assert depth is not None
    allocated = _count(allocated_value, "allocated_bytes")
    direct = _count(direct_value, "direct_bytes")
    assert allocated is not None and direct is not None
    expected_depth = 0 if path == "/" else path_depth(path)
    if depth != expected_depth:
        raise _DiskFactsError(f"directory depth does not match path {path!r}")
    if depth > _MAX_EXTRA_DEPTH:
        raise _DiskFactsError("directory depth exceeds the wire bound")
    if depth > _REPORT_DEPTH and path not in extra_paths:
        raise _DiskFactsError(f"unsolicited deep directory path {path!r}")
    return DiskUsageDirectory(
        path=path,
        depth=depth,
        allocated_bytes=allocated,
        direct_bytes=direct,
    )


def _strict_parse(
    facts: Mapping[str, Any], extra_paths: Tuple[str, ...]
) -> DiskUsageFacts:
    if not isinstance(facts, Mapping):
        raise _DiskFactsError("disk-usage facts must be a mapping")
    try:
        status_value = facts["status"]
        used_value = facts["root_used_bytes"]
        device_value = facts["root_device"]
        directories_value = facts["directories"]
        visited_value = facts["visited_entries"]
        excluded_value = facts["excluded_mounts"]
        error_value = facts["error_count"]
        reason_value = facts["reason"]
    except KeyError as exc:
        raise _DiskFactsError("disk-usage facts are missing required fields") from exc

    if status_value not in ("complete", "partial", "unavailable"):
        raise _DiskFactsError("invalid disk-usage status")
    used = _count(used_value, "root_used_bytes", allow_none=True)
    device = _count(device_value, "root_device", allow_none=True)
    visited = _count(visited_value, "visited_entries")
    excluded = _count(excluded_value, "excluded_mounts")
    errors = _count(error_value, "error_count")
    assert visited is not None and excluded is not None and errors is not None
    if not isinstance(directories_value, (list, tuple)):
        raise _DiskFactsError("directories must be a list")
    if len(directories_value) > _MAX_WIRE_ROWS:
        raise _DiskFactsError("disk-usage row budget exceeded")
    if not isinstance(reason_value, str):
        raise _DiskFactsError("reason must be a string")

    wanted = set(extra_paths)
    rows: List[DiskUsageDirectory] = []
    seen_paths: Set[str] = set()
    for row in directories_value:
        directory = _parse_directory(row, extra_paths=wanted)
        if directory.path in seen_paths:
            raise _DiskFactsError(f"duplicate directory path {directory.path!r}")
        seen_paths.add(directory.path)
        rows.append(directory)

    if status_value != "unavailable" and "/" not in seen_paths:
        # Every measured snapshot reserves a root row; without it the row set
        # cannot be attributed to the measured filesystem, and ancestor
        # lookups have no terminal path to stop at.
        raise _DiskFactsError("disk-usage rows are missing the filesystem root")

    return DiskUsageFacts(
        status=status_value,
        root_used_bytes=used,
        root_device=device,
        directories=tuple(rows),
        visited_entries=visited,
        excluded_mounts=excluded,
        error_count=errors,
        reason=_bounded_detail(reason_value, "") if reason_value else "",
        extra_paths=extra_paths,
    )


def parse_disk_usage_facts(
    facts: Mapping[str, Any], *, extra_paths: Sequence[str] = ()
) -> DiskUsageFacts:
    """Validate raw ``RunnerExecutor.lxc_disk_usage`` facts strictly.

    Returns an immutable typed snapshot.  Any malformed value, unsolicited
    deep row, duplicate path or bound violation yields an ``unavailable``
    snapshot with a bounded reason instead of raising; callers keep the
    original warning untouched.
    """
    try:
        normalized_extra = _normalize_extra_paths(extra_paths)
        return _strict_parse(facts, normalized_extra)
    except _DiskFactsError as exc:
        return _unavailable_facts(str(exc))


# --------------------------------------------------------------------------- #
# Ratio filter
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Consumer:
    path: str
    size: int
    cap: float
    kind: Literal["directory", "directory-other", "root-files"]


@dataclass(frozen=True)
class _Subtree:
    consumers: Tuple[_Consumer, ...]
    covered_bytes: int
    has_requested: bool
    has_normal: bool


def _unavailable_insight(
    used_bytes: Optional[int], reason: str
) -> DiskUsageInsight:
    return DiskUsageInsight(
        status="unavailable",
        used_bytes=used_bytes,
        entries=[],
        omitted=0,
        detail=_bounded_detail(reason, "disk measurement unavailable"),
    )


def _cap_for(path: str, policy: DiskUsagePolicy) -> float:
    cap = policy.expected_share_percent(path)
    if cap is not None:
        return cap
    default = policy.default_max_share_percent
    if default is None:
        return _DEFAULT_CAP
    return default


def _floor_bytes(policy: DiskUsagePolicy) -> int:
    floor_mb = policy.min_directory_mb
    if floor_mb is None:
        floor_mb = _DEFAULT_FLOOR_MB
    return int(floor_mb) * 1024 * 1024


def _exceeds_share(size: int, cap: float, used: int) -> bool:
    return Fraction(size * 100, used) > Fraction(cap)


def _qualifies(size: int, cap: float, floor: int, used: int) -> bool:
    return size > 0 and size >= floor and _exceeds_share(size, cap, used)


def _nearest_present_parent(
    path: str, paths: Set[str]
) -> Optional[str]:
    current = path
    while True:
        parent = posixpath.dirname(current)
        if parent == current or parent in ("", "/"):
            # The filesystem root is the terminal ancestor.  Stop even when the
            # row set omitted it so malformed input cannot loop forever.
            return "/" if "/" in paths else None
        if parent in paths:
            return parent
        current = parent


def _assert_consistent(facts: DiskUsageFacts, used: int) -> None:
    by_path = {row.path: row for row in facts.directories}
    paths = set(by_path)
    for row in facts.directories:
        if row.direct_bytes > row.allocated_bytes:
            raise _InconsistentMeasurements(
                "direct bytes exceed the directory total"
            )
        if row.allocated_bytes > used:
            raise _InconsistentMeasurements(
                "measured bytes exceed the used-space denominator"
            )
        if row.path == "/":
            # The helper's root row is a direct-files bucket, not the total.
            continue
        parent = _nearest_present_parent(row.path, paths)
        if parent is not None and parent != "/":
            if row.allocated_bytes > by_path[parent].allocated_bytes:
                raise _InconsistentMeasurements(
                    "child aggregate exceeds its ancestor"
                )


def _select_subtree(
    path: str,
    *,
    by_path: Mapping[str, DiskUsageDirectory],
    children: Mapping[str, Tuple[str, ...]],
    policy: DiskUsagePolicy,
    requested: Mapping[str, bool],
    floor: int,
    used: int,
    allow_normal_coverage: bool,
) -> _Subtree:
    child_results = [
        _select_subtree(
            child,
            by_path=by_path,
            children=children,
            policy=policy,
            requested=requested,
            floor=floor,
            used=used,
            allow_normal_coverage=allow_normal_coverage,
        )
        for child in children.get(path, ())
    ]
    consumers: List[_Consumer] = []
    for result in child_results:
        consumers.extend(result.consumers)
    covered = sum(result.covered_bytes for result in child_results)
    has_requested = any(result.has_requested for result in child_results)
    has_normal = any(result.has_normal for result in child_results)

    if path == "/":
        # Never report the root itself; its direct files are handled separately.
        return _Subtree(tuple(consumers), 0, has_requested, has_normal)

    row = by_path[path]
    size = row.allocated_bytes
    cap = _cap_for(path, policy)
    self_requested = requested.get(path, False)
    # Only a path declared directly in the policy is expected-normal coverage.
    # An inherited cap still gates eligibility, but a descendant row cannot
    # speak for the whole declared subtree. A partial scan is a lower bound, so
    # a measured below-cap subtree is not proof of normal coverage; disabling
    # the inference there keeps a potentially larger real culprit visible.
    explicit = policy.path_max_share_percent.get(path)
    if (
        allow_normal_coverage
        and explicit is not None
        and Fraction(size * 100, used) <= Fraction(explicit)
    ):
        # Explicit expected-normal subtree: keep more-specific qualifying
        # descendants, but never let the covered bytes reappear in an ancestor.
        covered_here = size - sum(consumer.size for consumer in consumers)
        if covered_here < 0:
            raise _InconsistentMeasurements("normal subtree total is negative")
        return _Subtree(tuple(consumers), covered_here, has_requested, True)

    if not _qualifies(size, cap, floor, used):
        return _Subtree(tuple(consumers), covered, has_requested, has_normal)

    keep_requested = has_requested or self_requested
    frontier = sum(consumer.size for consumer in consumers)
    if covered > 0 or keep_requested or has_normal:
        # A specifically requested deep path, an expected-normal subtree or
        # already-covered bytes take precedence over the ordinary 70% collapse:
        # this parent may only contribute the bytes not credited below, even
        # when its specific frontier covers >= 70% of its total.  The ancestor
        # need not be requested itself; a retained qualifying requested
        # descendant forces this residual accounting on every eligible
        # ancestor, and the request flag keeps propagating upwards.
        residual = size - frontier - covered
        if residual < 0:
            raise _InconsistentMeasurements("residual bytes are negative")
        if _qualifies(residual, cap, floor, used):
            kind: Literal["directory", "directory-other"] = (
                "directory-other" if residual < size else "directory"
            )
            consumers.append(
                _Consumer(
                    path=path,
                    size=residual,
                    cap=cap,
                    kind=kind,
                )
            )
        return _Subtree(tuple(consumers), covered, keep_requested, has_normal)

    if frontier * 100 >= size * 70:
        # The specific frontier dominates: report it instead of this parent.
        return _Subtree(tuple(consumers), covered, keep_requested, has_normal)

    # Ordinary collapse: the parent is the coarser culprit; its whole subtree
    # is already represented by the parent's bytes.
    consumer = _Consumer(path=path, size=size, cap=cap, kind="directory")
    return _Subtree((consumer,), 0, False, False)


def _filter_strict(facts: DiskUsageFacts, policy: DiskUsagePolicy) -> DiskUsageInsight:
    if facts.status == "unavailable":
        return _unavailable_insight(
            facts.root_used_bytes,
            facts.reason or "disk measurement unavailable",
        )

    used = facts.root_used_bytes
    if used is None or used <= 0:
        return _unavailable_insight(
            facts.root_used_bytes if used is not None else None,
            "used space unavailable" if used is None else "used space is zero",
        )

    status = facts.status

    by_path = {row.path: row for row in facts.directories}
    if "/" not in by_path:
        # Directly constructed typed facts can bypass the parser; a row set
        # without its root cannot be filtered or attributed safely.
        return _unavailable_insight(
            used, "measurements are missing the filesystem root"
        )

    try:
        _assert_consistent(facts, used)
    except _InconsistentMeasurements:
        return DiskUsageInsight(
            status="partial",
            used_bytes=used,
            entries=[],
            omitted=0,
            detail="measurements inconsistent; shares unavailable",
        )

    paths = set(by_path)
    children_map: Dict[str, List[str]] = {}
    for path in sorted(by_path, key=lambda item: (_depth_of(item), item)):
        if path == "/":
            continue
        parent = _nearest_present_parent(path, paths)
        if parent is None:
            parent = "/"
        children_map.setdefault(parent, []).append(path)
    children: Dict[str, Tuple[str, ...]] = {
        parent: tuple(items) for parent, items in children_map.items()
    }

    requested = {
        row.path: True
        for row in facts.directories
        if row.depth > _REPORT_DEPTH
    }
    floor = _floor_bytes(policy)
    allow_normal_coverage = facts.status == "complete"
    result = _select_subtree(
        "/",
        by_path=by_path,
        children=children,
        policy=policy,
        requested=requested,
        floor=floor,
        used=used,
        allow_normal_coverage=allow_normal_coverage,
    )

    consumers = list(result.consumers)
    root_row = by_path.get("/")
    if root_row is not None:
        root_cap = _cap_for("/", policy)
        if _qualifies(root_row.direct_bytes, root_cap, floor, used):
            consumers.append(
                _Consumer(
                    path="/",
                    size=root_row.direct_bytes,
                    cap=root_cap,
                    kind="root-files",
                )
            )

    seen: Set[str] = set()
    entries: List[DiskUsageEntry] = []
    for consumer in consumers:
        if consumer.path in seen:
            raise _InconsistentMeasurements("duplicate consumer path")
        seen.add(consumer.path)
        share = 100.0 * consumer.size / used
        if not 0.0 <= share <= 100.0:
            raise _InconsistentMeasurements("share outside 0..100")
        entries.append(
            DiskUsageEntry(
                path=consumer.path,
                allocated_bytes=consumer.size,
                share_percent=share,
                max_expected_share_percent=consumer.cap,
                kind=consumer.kind,
            )
        )

    entries.sort(
        key=lambda entry: (
            -entry.share_percent,
            -entry.allocated_bytes,
            entry.path,
            entry.kind,
        )
    )
    omitted = max(0, len(entries) - _MAX_ENTRIES)
    kept = entries[:_MAX_ENTRIES]
    if status == "partial":
        # The formatter owns the incomplete-coverage safety statement; the
        # insight carries the collector's sanitized cause (or nothing) so
        # callers can tell why the scan stopped without duplicate prose.
        detail = _bounded_detail(facts.reason, "")
    else:
        detail = "" if kept else "no directory exceeds its expected share"
    return DiskUsageInsight(
        status=status,
        used_bytes=used,
        entries=kept,
        omitted=omitted,
        detail=detail,
    )


def _depth_of(path: str) -> int:
    return 0 if path == "/" else path_depth(path)


def filter_disk_usage(
    facts: DiskUsageFacts, *, policy: DiskUsagePolicy
) -> DiskUsageInsight:
    """Filter validated facts into bounded, disjoint, typed consumers.

    Shares are of the measured used-space denominator.  Parent/child overlap is
    collapsed bottom-up (a >=70% specific frontier replaces an ordinary
    parent), while explicitly declared expected-normal subtrees are subtracted
    from coarser ancestors and requested deep paths are retained as specifics
    with their overlapping ancestor reduced to a ``directory-other`` residual.
    Requested and expected-normal residuals take precedence over the ordinary
    70% collapse so every retained byte has one owner.

    A partial snapshot is a lower bound, so its measured below-cap subtrees are
    not treated as proven expected-normal coverage; only complete snapshots may
    apply that normal-budget inference. Partial results keep the ``partial``
    status and carry the collector's bounded reason as ``detail`` when present.
    """
    if not isinstance(facts, DiskUsageFacts):
        return _unavailable_insight(None, "malformed disk-usage facts")
    try:
        return _filter_strict(facts, policy)
    except _InconsistentMeasurements as exc:
        used = facts.root_used_bytes if facts.root_used_bytes else None
        return DiskUsageInsight(
            status="partial",
            used_bytes=used,
            entries=[],
            omitted=0,
            detail=_bounded_detail(
                f"measurements inconsistent; shares unavailable ({exc})",
                "measurements inconsistent; shares unavailable",
            ),
        )


# --------------------------------------------------------------------------- #
# Collection coordinator
# --------------------------------------------------------------------------- #


def collect_lxc_disk_insight(
    executor: "Executor",
    lxc_id: str,
    *,
    df_stdout: str,
    policy: DiskUsagePolicy,
) -> DiskUsageInsight:
    """Request one disk measurement and filter it for the existing warning.

    A failed, timed-out, missing or malformed measurement returns an
    ``unavailable`` insight so the caller keeps the original low-space warning.
    Arbitrary CLI stderr is never echoed into the insight.
    """
    try:
        extra_paths = disk_usage_extra_paths(policy)
        with _COLLECTOR_SEMAPHORE:
            result = executor.lxc_disk_usage(lxc_id, extra_paths=extra_paths)
        if not result.ok:
            return _unavailable_insight(None, "disk measurement unavailable")
        raw_facts = result.facts
    except Exception:  # diagnostics must never fail the update
        return _unavailable_insight(None, "disk measurement unavailable")

    facts = parse_disk_usage_facts(raw_facts, extra_paths=extra_paths)
    if facts.status != "unavailable" and facts.root_used_bytes is None:
        df_used_kb = (
            parse_df_used_kb(df_stdout) if isinstance(df_stdout, str) else None
        )
        if df_used_kb is not None and df_used_kb <= _MAX_INT // 1024:
            facts = replace(facts, root_used_bytes=df_used_kb * 1024)
    try:
        return filter_disk_usage(facts, policy=policy)
    except Exception:  # diagnostics must never fail the update
        return _unavailable_insight(
            facts.root_used_bytes, "disk measurement unavailable"
        )


__all__ = [
    "DiskUsageDirectory",
    "DiskUsageFacts",
    "collect_lxc_disk_insight",
    "filter_disk_usage",
    "parse_disk_usage_facts",
]
