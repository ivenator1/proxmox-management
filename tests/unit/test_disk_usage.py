"""Behavioral tests for the bounded root-filesystem disk diagnostics.

Every case uses the real strict parser, the real filter and the real collector
with plain fact mappings and a scripted executor.  No helper process, guest or
filesystem walk is involved.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from proxmox_fleet.disk_usage import (
    DiskUsageDirectory,
    DiskUsageFacts,
    collect_lxc_disk_insight,
    filter_disk_usage,
    parse_disk_usage_facts,
)
from proxmox_fleet.log_plugins import (
    disk_usage_extra_paths,
    resolve_disk_usage_policy,
)
from proxmox_fleet.lxc_parse import parse_df_used_kb
from proxmox_fleet.models.log_plugins import DiskUsagePolicy
from proxmox_fleet.models.state import DiskUsageEntry, DiskUsageInsight
from proxmox_fleet.runner import PrimitiveResult

GIB = 1024**3
MIB = 1024**2
STORE = "/root/.local/share/pnpm/store"


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


def _depth(path: str) -> int:
    return 0 if path == "/" else len([part for part in path.split("/") if part])


def _dir(
    path: str,
    allocated: int,
    *,
    direct: int = 0,
    depth: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "path": path,
        "depth": _depth(path) if depth is None else depth,
        "allocated_bytes": allocated,
        "direct_bytes": direct,
    }


def _facts(
    *,
    used: Optional[int] = 20 * GIB,
    rows: Sequence[Dict[str, Any]] = (),
    root_direct: int = 0,
    root_allocated: Any = None,
    status: str = "complete",
    error_count: int = 0,
    reason: str = "",
) -> Dict[str, Any]:
    if root_allocated is None:
        root_total = root_direct
    elif root_allocated == "used":
        root_total = used or 0
    else:
        root_total = int(root_allocated)
    directories: List[Dict[str, Any]] = [
        {
            "path": "/",
            "depth": 0,
            "allocated_bytes": root_total,
            "direct_bytes": root_direct,
        }
    ]
    directories.extend(rows)
    return {
        "status": status,
        "root_used_bytes": used,
        "root_device": 0xD15C,
        "directories": directories,
        "visited_entries": len(directories),
        "excluded_mounts": 2,
        "error_count": error_count,
        "reason": reason,
    }


def _policy(
    cap: float = 20.0,
    *,
    floor: int = 0,
    caps: Optional[Dict[str, float]] = None,
) -> DiskUsagePolicy:
    return DiskUsagePolicy(
        default_max_share_percent=cap,
        min_directory_mb=floor,
        path_max_share_percent={} if caps is None else caps,
    )


def _facts_for(policy: DiskUsagePolicy, **kwargs: Any) -> DiskUsageFacts:
    extras = disk_usage_extra_paths(policy)
    parsed = parse_disk_usage_facts(_facts(**kwargs), extra_paths=extras)
    assert parsed.status != "unavailable", parsed.reason
    return parsed


def _insight(policy: DiskUsagePolicy, **kwargs: Any) -> DiskUsageInsight:
    return filter_disk_usage(_facts_for(policy, **kwargs), policy=policy)


def _entries(insight: DiskUsageInsight) -> Dict[str, DiskUsageEntry]:
    return {entry.path: entry for entry in insight.entries}


def _df_row(used_kb: int, *, total_kb: Optional[int] = None) -> str:
    total = used_kb if total_kb is None else total_kb
    percent = 0 if total <= 0 else min(100, int(round(used_kb * 100 / total)))
    return (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        f"/dev/disk-smoke {total} {used_kb} 0 {percent}% /\n"
    )


class _FakeExecutor:
    host = "pve01"

    def __init__(
        self,
        result: Optional[PrimitiveResult] = None,
        *,
        error: Optional[BaseException] = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: List[Tuple[str, Tuple[str, ...]]] = []

    def lxc_disk_usage(
        self, lxc_id: str, *, extra_paths: Sequence[str] = ()
    ) -> PrimitiveResult:
        self.calls.append((lxc_id, tuple(extra_paths)))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


# --------------------------------------------------------------------------- #
# df used-column parser (fallback denominator only)
# --------------------------------------------------------------------------- #


def test_parse_df_used_kb_reads_column_two_and_fails_closed():
    stdout = (
        "Filesystem     1024-blocks    Used Available Capacity Mounted on\n"
        "/dev/rbd17         4046560 3416796    403668      90% /\n"
    )
    assert parse_df_used_kb(stdout) == 3416796
    assert parse_df_used_kb("") is None
    assert parse_df_used_kb("garbage") is None
    assert parse_df_used_kb("Filesystem 1024-blocks Used Available Capacity Mounted on") is None
    assert parse_df_used_kb("/dev/x 100 -1 0 0% /\n") is None
    assert parse_df_used_kb("/dev/x 100 abc 0 0% /\n") is None


# --------------------------------------------------------------------------- #
# Strict wire parser
# --------------------------------------------------------------------------- #


def test_parse_disk_usage_facts_is_typed_immutable_and_keeps_measurements():
    raw = _facts(
        rows=[_dir("/var", 5 * GIB, direct=GIB), _dir(STORE, 4 * GIB, depth=5)]
    )
    raw["ignored_transport_key"] = {"plugin": "x"}
    facts = parse_disk_usage_facts(raw, extra_paths=[STORE])

    assert isinstance(facts, DiskUsageFacts)
    assert facts.status == "complete"
    assert facts.root_used_bytes == 20 * GIB
    assert facts.root_device == 0xD15C
    assert tuple(row.path for row in facts.directories) == ("/", "/var", STORE)
    assert facts.directories[1].allocated_bytes == 5 * GIB
    assert facts.directories[1].direct_bytes == GIB
    assert facts.directories[2].depth == 5
    assert facts.extra_paths == (STORE,)
    assert facts.visited_entries == 3
    assert facts.excluded_mounts == 2
    assert facts.error_count == 0

    with pytest.raises(dataclasses.FrozenInstanceError):
        facts.status = "partial"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        facts.directories[0].path = "/other"  # type: ignore[misc]


def test_parse_disk_usage_facts_sorts_extras():
    facts = parse_disk_usage_facts(
        _facts(),
        extra_paths=["/z/z/z/z/z", "/a/a/a/a/a"],
    )
    assert facts.extra_paths == ("/a/a/a/a/a", "/z/z/z/z/z")


def test_parse_disk_usage_facts_accepts_full_row_and_extra_budgets():
    extras = [f"/e{index:02d}/x/y/z/w" for index in range(32)]
    ordinary = [_dir(f"/d{index:03d}", 1) for index in range(967)]
    rows = [_dir(path, 1, depth=5) for path in extras] + ordinary
    raw = _facts(rows=rows)
    assert len(raw["directories"]) == 1000
    facts = parse_disk_usage_facts(raw, extra_paths=extras)
    assert facts.status == "complete"
    assert len(facts.directories) == 1000
    assert facts.extra_paths == tuple(sorted(extras))


def test_parse_disk_usage_facts_preserves_zero_used_and_absent_extras():
    facts = parse_disk_usage_facts(
        _facts(used=0, rows=[_dir("/var", MIB)]),
        extra_paths=[STORE],
    )
    assert facts.status == "complete"
    assert facts.root_used_bytes == 0
    # A requested extra that was not visited is unknown, never fabricated as 0.
    assert STORE not in {row.path for row in facts.directories}


def test_parse_disk_usage_facts_sanitizes_and_bounds_reasons():
    raw = _facts(
        used=None,
        status="unavailable",
        reason="pct\nmissing\x00 " + "x" * 400,
    )
    facts = parse_disk_usage_facts(raw)
    assert facts.status == "unavailable"
    assert facts.root_used_bytes is None
    assert len(facts.reason) <= 160
    assert "\n" not in facts.reason
    assert "\x00" not in facts.reason
    insight = filter_disk_usage(facts, policy=_policy())
    assert insight.status == "unavailable"
    assert insight.entries == []
    assert len(insight.detail) <= 160
    assert "\n" not in insight.detail


def test_parse_disk_usage_facts_rejects_malformed_input_without_raising():
    base = _facts(rows=[_dir("/var", GIB), _dir("/var/log", MIB)])
    deep = _dir(STORE, GIB, depth=5)
    many_rows = [_dir(f"/d{index:04d}", 1) for index in range(1001)]

    cases: List[Tuple[str, Any, Sequence[str]]] = [
        ("not a mapping", ["nope"], ()),
        ("missing reason", {key: value for key, value in base.items() if key != "reason"}, ()),
        ("bad status", {**base, "status": "bogus"}, ()),
        ("bool used", {**base, "root_used_bytes": True}, ()),
        ("negative used", {**base, "root_used_bytes": -1}, ()),
        ("bad device", {**base, "root_device": "x"}, ()),
        ("bad directories", {**base, "directories": "bad"}, ()),
        ("row budget", {**base, "directories": many_rows}, ()),
        ("row not mapping", {**base, "directories": ["bad"]}, ()),
        ("row missing field", {**base, "directories": [{"path": "/var"}]}, ()),
        ("negative bytes", {**base, "directories": [_dir("/var", -1)]}, ()),
        ("bool bytes", {**base, "directories": [_dir("/var", True)]}, ()),
        (
            "depth mismatch",
            {**base, "directories": [_dir("/var/log", GIB, depth=1)]},
            (),
        ),
        (
            "duplicate path",
            {**base, "directories": [_dir("/var", GIB), _dir("/var", GIB)]},
            (),
        ),
        ("relative path", {**base, "directories": [_dir("var/log", GIB)]}, ()),
        ("dotdot path", {**base, "directories": [_dir("/var/../etc", GIB)]}, ()),
        ("unnormalized path", {**base, "directories": [_dir("/var//log", GIB)]}, ()),
        ("double leading slash", {**base, "directories": [_dir("//var/log", GIB)]}, ()),
        ("trailing slash", {**base, "directories": [_dir("/var/log/", GIB)]}, ()),
        ("glob path", {**base, "directories": [_dir("/var/*", GIB)]}, ()),
        ("NUL path", {**base, "directories": [_dir("/var/\x00log", GIB)]}, ()),
        ("URI path", {**base, "directories": [_dir("file:///var/log", GIB)]}, ()),
        ("unsolicited deep row", {**base, "directories": [deep]}, ()),
        (
            "unrequested deep row",
            {**base, "directories": [_dir("/other/x/y/z/w", GIB, depth=5)]},
            [STORE],
        ),
        (
            "missing root complete",
            {**base, "directories": [_dir("/var", GIB), _dir("/var/log", MIB)]},
            (),
        ),
        (
            "missing root partial",
            {
                **base,
                "status": "partial",
                "directories": [_dir("/var", GIB), _dir("/var/log", MIB)],
            },
            (),
        ),
        ("empty rows complete", {**base, "directories": []}, ()),
        ("bool count", {**base, "visited_entries": True}, ()),
        ("negative count", {**base, "excluded_mounts": -1}, ()),
        ("bad reason", {**base, "reason": 5}, ()),
        ("duplicate extras", base, ["/a/b/c/d/e", "/a/b/c/d/e"]),
        ("too many extras", base, [f"/x{i}/b/c/d/e" for i in range(33)]),
        ("root extra", base, ["/"]),
        ("relative extra", base, ["a/b/c/d/e"]),
        ("glob extra", base, ["/a/*/c/d/e"]),
        ("URI extra", base, ["file:///a/b/c/d"]),
        ("deep extra", base, ["/" + "/".join(["x"] * 17)]),
        ("long extra", base, ["/" + "x" * 600]),
    ]
    for label, raw, extras in cases:
        parsed = parse_disk_usage_facts(raw, extra_paths=extras)
        assert parsed.status == "unavailable", label
        assert parsed.directories == (), label
        assert parsed.reason, label
        assert len(parsed.reason) <= 160, label


# --------------------------------------------------------------------------- #
# Ratio filter: shares, boundaries and service caps
# --------------------------------------------------------------------------- #


def test_filter_uses_used_space_and_collapses_parent_duplicates():
    policy = _policy(floor=0)
    insight = _insight(
        policy,
        rows=[
            _dir("/var", 18 * GIB),
            _dir("/var/log", 14 * GIB),
            _dir("/var/cache", GIB),
        ],
    )
    assert insight.status == "complete"
    assert insight.used_bytes == 20 * GIB
    assert insight.omitted == 0
    assert [entry.path for entry in insight.entries] == ["/var/log"]
    entry = insight.entries[0]
    assert entry.kind == "directory"
    assert entry.allocated_bytes == 14 * GIB
    assert entry.share_percent == pytest.approx(70.0)
    assert entry.max_expected_share_percent == 20.0


def test_filter_strict_ratio_boundary_and_floor_semantics():
    policy = _policy(floor=0)
    exact = _insight(policy, rows=[_dir("/boundary", 4 * GIB)])
    assert exact.entries == []
    over = _insight(policy, rows=[_dir("/boundary", 4 * GIB + 1)])
    assert [entry.path for entry in over.entries] == ["/boundary"]
    assert over.entries[0].share_percent > 20.0

    floored = _insight(
        _policy(floor=256),
        used=GIB,
        rows=[_dir("/small", 256 * MIB)],
    )
    assert [entry.path for entry in floored.entries] == ["/small"]
    below = _insight(
        _policy(floor=256),
        used=GIB,
        rows=[_dir("/small", 256 * MIB - 1)],
    )
    assert below.entries == []

    zero_floor = _insight(
        _policy(floor=0),
        used=2 * MIB,
        rows=[_dir("/small", 512 * 1024)],
    )
    assert [entry.path for entry in zero_floor.entries] == ["/small"]


def test_filter_generic_usr_cap_inherits_but_does_not_cover_descendants():
    policy, finding = resolve_disk_usage_policy(
        None, script_id=None, override=None, auto=True
    )
    assert finding
    # /usr is over its 85% expected cap; /usr/share inherits the cap (25% is
    # normal) but inherited caps are not explicit expected-normal coverage.
    insight = _insight(
        policy,
        rows=[
            _dir("/usr", 18 * GIB),
            _dir("/usr/share", 5 * GIB),
        ],
    )
    entries = _entries(insight)
    assert "/usr/share" not in entries
    assert list(entries) == ["/usr"]
    assert entries["/usr"].share_percent == pytest.approx(90.0)
    assert entries["/usr"].max_expected_share_percent == 85.0
    assert entries["/usr"].kind == "directory"

    inherited = _insight(policy, rows=[_dir("/usr/share", 18 * GIB)])
    inherited_entries = _entries(inherited)
    assert list(inherited_entries) == ["/usr/share"]
    assert inherited_entries["/usr/share"].share_percent == pytest.approx(90.0)
    assert inherited_entries["/usr/share"].max_expected_share_percent == 85.0


def test_filter_longest_prefix_cap_wins_and_normal_coverage_propagates():
    policy = _policy(caps={"/app": 50.0, "/app/data": 25.0})
    insight = _insight(
        policy,
        used=100 * GIB,
        rows=[
            _dir("/app", 40 * GIB),
            _dir("/app/data", 30 * GIB),
        ],
    )
    entries = _entries(insight)
    assert list(entries) == ["/app/data"]
    assert entries["/app/data"].share_percent == pytest.approx(30.0)
    assert entries["/app/data"].max_expected_share_percent == 25.0


def test_filter_normal_database_never_reappears_as_a_coarse_ancestor():
    policy = _policy(caps={"/var/lib/postgresql": 85.0})
    quiet = _insight(
        policy,
        rows=[
            _dir("/var", 17 * GIB),
            _dir("/var/lib", 14 * GIB),
            _dir("/var/lib/postgresql", 14 * GIB),
            _dir("/var/log", 3 * GIB),
        ],
    )
    assert quiet.entries == []
    assert quiet.status == "complete"

    over = _insight(
        policy,
        rows=[
            _dir("/var", 20 * GIB),
            _dir("/var/lib", 18 * GIB),
            _dir("/var/lib/postgresql", 18 * GIB),
            _dir("/var/log", 2 * GIB),
        ],
    )
    entries = _entries(over)
    assert list(entries) == ["/var/lib/postgresql"]
    assert entries["/var/lib/postgresql"].share_percent == pytest.approx(90.0)
    assert entries["/var/lib/postgresql"].kind == "directory"

    # A partial scan is a lower bound: the measured 70% subtree is not proof
    # of normal coverage, so the measured oversized ancestor stays visible.
    partial = _insight(
        policy,
        status="partial",
        reason="deadline",
        rows=[
            _dir("/var", 17 * GIB),
            _dir("/var/lib", 14 * GIB),
            _dir("/var/lib/postgresql", 14 * GIB),
            _dir("/var/log", 3 * GIB),
        ],
    )
    partial_entries = _entries(partial)
    assert partial.status == "partial"
    assert list(partial_entries) == ["/var/lib"]
    assert partial_entries["/var/lib"].share_percent == pytest.approx(70.0)
    assert "incomplete" in partial.detail.lower()


def test_filter_normal_subtree_residual_is_directory_other_not_full_parent():
    policy = _policy(caps={"/var/lib/postgresql": 85.0})
    insight = _insight(
        policy,
        rows=[
            _dir("/var", 20 * GIB),
            _dir("/var/lib/postgresql", 14 * GIB),
        ],
    )
    entries = _entries(insight)
    assert set(entries) == {"/var"}
    parent = entries["/var"]
    assert parent.kind == "directory-other"
    assert parent.allocated_bytes == 6 * GIB
    assert parent.share_percent == pytest.approx(30.0)
    assert "/var/lib/postgresql" not in entries


def test_filter_deep_requested_cache_keeps_specific_row_and_residual_parent():
    policy = _policy(
        caps={"/root/.local/share": 20.0, STORE: 20.0}
    )
    store_only = _insight(
        policy,
        used=100 * GIB,
        rows=[
            _dir("/root/.local/share", 40 * GIB),
            _dir(STORE, 22 * GIB, depth=5),
        ],
    )
    entries = _entries(store_only)
    assert set(entries) == {STORE}
    assert entries[STORE].kind == "directory"
    assert entries[STORE].share_percent == pytest.approx(22.0)

    parent_residual = _insight(
        policy,
        used=100 * GIB,
        rows=[
            _dir("/root/.local/share", 80 * GIB),
            _dir(STORE, 22 * GIB, depth=5),
        ],
    )
    entries = _entries(parent_residual)
    assert set(entries) == {"/root/.local/share", STORE}
    assert entries[STORE].kind == "directory"
    assert entries[STORE].share_percent == pytest.approx(22.0)
    parent = entries["/root/.local/share"]
    assert parent.kind == "directory-other"
    assert parent.allocated_bytes == 58 * GIB
    assert parent.share_percent == pytest.approx(58.0)
    assert sum(entry.allocated_bytes for entry in entries.values()) == 80 * GIB


def test_filter_nested_requested_paths_are_disjoint_and_deepest_wins():
    policy = _policy(caps={STORE: 20.0, STORE + "/tmp": 20.0})
    nested = _insight(
        policy,
        used=100 * GIB,
        rows=[
            _dir(STORE, 60 * GIB, depth=5),
            _dir(STORE + "/tmp", 25 * GIB, depth=6),
        ],
    )
    entries = _entries(nested)
    assert set(entries) == {STORE, STORE + "/tmp"}
    assert entries[STORE + "/tmp"].kind == "directory"
    assert entries[STORE + "/tmp"].share_percent == pytest.approx(25.0)
    assert entries[STORE].kind == "directory-other"
    assert entries[STORE].share_percent == pytest.approx(35.0)
    assert sum(entry.allocated_bytes for entry in entries.values()) == 60 * GIB

    shallow = _insight(
        policy,
        used=100 * GIB,
        rows=[
            _dir(STORE, 40 * GIB, depth=5),
            _dir(STORE + "/tmp", 25 * GIB, depth=6),
        ],
    )
    assert list(_entries(shallow)) == [STORE + "/tmp"]


def test_filter_requested_parent_residual_precedes_seventy_percent_collapse():
    parent_path = "/root/.local/share/pnpm"
    policy = _policy(caps={parent_path: 5.0, STORE: 20.0})
    insight = _insight(
        policy,
        used=100 * GIB,
        rows=[
            _dir(parent_path, 30 * GIB, depth=4),
            _dir(STORE, 22 * GIB, depth=5),
        ],
    )
    entries = _entries(insight)
    # The child store covers 73% of its requested parent: the parent's
    # qualifying residual is still reported as a disjoint consumer.
    assert set(entries) == {parent_path, STORE}
    parent = entries[parent_path]
    assert parent.kind == "directory-other"
    assert parent.allocated_bytes == 8 * GIB
    assert parent.share_percent == pytest.approx(8.0)
    assert parent.max_expected_share_percent == 5.0
    assert entries[STORE].kind == "directory"
    assert entries[STORE].share_percent == pytest.approx(22.0)
    assert sum(entry.allocated_bytes for entry in entries.values()) == 30 * GIB

    # A residual at or below the parent's cap stays omitted even when the
    # specific child dominates the parent.
    below = _insight(
        policy,
        used=100 * GIB,
        rows=[
            _dir(parent_path, 30 * GIB, depth=4),
            _dir(STORE, 29 * GIB, depth=5),
        ],
    )
    below_entries = _entries(below)
    assert set(below_entries) == {STORE}
    assert below_entries[STORE].share_percent == pytest.approx(29.0)

    # The coarse ancestor need not be requested itself: a retained qualifying
    # deep path forces its residual accounting wherever the ancestor sits.
    coarse_policy = _policy(cap=5.0, caps={STORE: 20.0})
    coarse_parent = "/root/.local/share"
    coarse = _insight(
        coarse_policy,
        used=100 * GIB,
        rows=[
            _dir(coarse_parent, 30 * GIB, depth=3),
            _dir(STORE, 22 * GIB, depth=5),
        ],
    )
    assert disk_usage_extra_paths(coarse_policy) == (STORE,)
    coarse_entries = _entries(coarse)
    assert set(coarse_entries) == {coarse_parent, STORE}
    assert coarse_entries[coarse_parent].kind == "directory-other"
    assert coarse_entries[coarse_parent].allocated_bytes == 8 * GIB
    assert coarse_entries[coarse_parent].max_expected_share_percent == 5.0
    assert coarse_entries[STORE].kind == "directory"
    assert coarse_entries[STORE].share_percent == pytest.approx(22.0)
    assert sum(entry.allocated_bytes for entry in coarse_entries.values()) == 30 * GIB


def test_filter_missing_root_is_unavailable_and_never_hangs():
    policy = _policy(floor=0)
    for status in ("complete", "partial"):
        facts = DiskUsageFacts(
            status=status,
            root_used_bytes=20 * GIB,
            root_device=0xD15C,
            directories=(
                DiskUsageDirectory(
                    path="/var",
                    depth=1,
                    allocated_bytes=GIB,
                    direct_bytes=0,
                ),
            ),
            visited_entries=1,
            excluded_mounts=0,
            error_count=0,
            reason="",
            extra_paths=(),
        )
        insight = filter_disk_usage(facts, policy=policy)
        assert insight.status == "unavailable", status
        assert insight.entries == [], status
        assert "root" in insight.detail, status

    # The ancestor walk stops at the filesystem root even without a root row.
    from proxmox_fleet.disk_usage import _nearest_present_parent

    assert _nearest_present_parent("/var/log", {"/var"}) == "/var"
    assert _nearest_present_parent("/var/log", {"/"}) == "/"
    assert _nearest_present_parent("/var/log", set()) is None
    assert _nearest_present_parent("/", {"/"}) == "/"
    assert _nearest_present_parent("/", set()) is None


def test_filter_ordinary_seventy_percent_frontier_and_coarse_fallback():
    policy = _policy(floor=0)
    frontier = _insight(
        policy,
        rows=[
            _dir("/var", 18 * GIB),
            _dir("/var/log", 14 * GIB),
            _dir("/var/cache", GIB),
        ],
    )
    assert [entry.path for entry in frontier.entries] == ["/var/log"]

    coarse = _insight(
        policy,
        rows=[
            _dir("/var/lib", 14 * GIB),
            _dir("/var/lib/a", 8 * GIB),
            _dir("/var/lib/b", 2 * GIB),
        ],
    )
    assert [entry.path for entry in coarse.entries] == ["/var/lib"]
    assert coarse.entries[0].share_percent == pytest.approx(70.0)


def test_filter_specific_descendant_survives_expected_normal_parent():
    policy = _policy(caps={"/data": 30.0, "/data/huge": 15.0})
    insight = _insight(
        policy,
        rows=[
            _dir("/data", 5 * GIB),
            _dir("/data/huge", 4 * GIB),
        ],
    )
    entries = _entries(insight)
    assert list(entries) == ["/data/huge"]
    assert entries["/data/huge"].share_percent == pytest.approx(20.0)
    assert entries["/data/huge"].max_expected_share_percent == 15.0


def test_filter_fully_credited_normal_subtree_never_becomes_a_parent_total():
    # The child needs its own lower cap: a descendant purely inside the
    # expected-normal subtree inherits its 100% cap and could never qualify.
    policy = _policy(
        caps={
            "/ancestor/normal": 100.0,
            "/ancestor/normal/child": 20.0,
        }
    )
    insight = _insight(
        policy,
        rows=[
            _dir("/ancestor", 20 * GIB),
            _dir("/ancestor/normal", 10 * GIB),
            _dir("/ancestor/normal/child", 10 * GIB),
        ],
    )
    entries = _entries(insight)
    assert set(entries) == {"/ancestor", "/ancestor/normal/child"}
    assert "/ancestor/normal" not in entries
    parent = entries["/ancestor"]
    assert parent.kind == "directory-other"
    assert parent.allocated_bytes == 10 * GIB
    assert parent.share_percent == pytest.approx(50.0)
    child = entries["/ancestor/normal/child"]
    assert child.kind == "directory"
    assert child.share_percent == pytest.approx(50.0)
    assert sum(entry.allocated_bytes for entry in entries.values()) == 20 * GIB

    # Partial snapshots cannot prove the below-cap subtree is normal, so the
    # conservative measured parent remains a positive oversized row.
    partial = _insight(
        policy,
        status="partial",
        rows=[
            _dir("/ancestor", 20 * GIB),
            _dir("/ancestor/normal", 10 * GIB),
            _dir("/ancestor/normal/child", 10 * GIB),
        ],
    )
    partial_entries = _entries(partial)
    assert partial.status == "partial"
    assert list(partial_entries) == ["/ancestor"]
    assert partial_entries["/ancestor"].kind == "directory"
    assert partial_entries["/ancestor"].allocated_bytes == 20 * GIB
    assert "incomplete" in partial.detail.lower()


def test_filter_nested_specific_inside_normal_subtree_is_retained():
    policy = _policy(
        caps={
            "/var/lib/postgresql": 85.0,
            "/var/lib/postgresql/wal": 15.0,
        }
    )
    insight = _insight(
        policy,
        rows=[
            _dir("/var", 14 * GIB),
            _dir("/var/lib/postgresql", 14 * GIB),
            _dir("/var/lib/postgresql/wal", 4 * GIB, depth=4),
        ],
    )
    entries = _entries(insight)
    assert list(entries) == ["/var/lib/postgresql/wal"]
    assert entries["/var/lib/postgresql/wal"].share_percent == pytest.approx(20.0)


def test_filter_root_files_bucket_is_never_a_root_total_and_ignores_root_layout():
    policy = _policy(floor=0)
    for root_allocated in (None, "used"):
        insight = _insight(
            policy,
            root_direct=5 * GIB,
            root_allocated=root_allocated,
        )
        assert len(insight.entries) == 1
        entry = insight.entries[0]
        assert entry.path == "/"
        assert entry.kind == "root-files"
        assert entry.allocated_bytes == 5 * GIB
        assert entry.share_percent == pytest.approx(25.0)
        assert entry.max_expected_share_percent == 20.0

    floored = _insight(
        _policy(floor=512),
        used=GIB,
        root_direct=300 * MIB,
    )
    assert floored.entries == []


def test_filter_inconsistent_measurements_are_partial_and_never_over_100():
    cases = [
        {"used": GIB, "rows": [_dir("/huge", 2 * GIB)]},
        {
            "used": 10 * GIB,
            "rows": [_dir("/parent", 4 * GIB), _dir("/parent/child", 5 * GIB)],
        },
        {"used": 10 * GIB, "rows": [_dir("/direct", 2 * GIB, direct=3 * GIB)]},
    ]
    policy = _policy(floor=0)
    for case in cases:
        insight = _insight(policy, **case)
        assert insight.status == "partial"
        assert insight.entries == []
        assert "unavailable" in insight.detail


def test_filter_zero_and_unknown_denominators_are_unavailable():
    policy = _policy(floor=0)
    zero = _insight(policy, used=0, rows=[_dir("/zero", MIB)])
    assert zero.status == "unavailable"
    assert zero.entries == []
    assert zero.used_bytes in (0, None)

    unknown = _insight(policy, used=None, rows=[_dir("/unknown", MIB)])
    assert unknown.status == "unavailable"
    assert unknown.entries == []
    assert unknown.used_bytes is None


def test_filter_complete_empty_partial_empty_and_partial_with_rows():
    policy = _policy(floor=0)
    complete = _insight(policy, rows=[_dir("/quiet", MIB)])
    assert complete.status == "complete"
    assert complete.entries == []
    assert complete.detail

    partial_empty = _insight(
        policy,
        status="partial",
        error_count=1,
        reason="deadline",
        rows=[_dir("/quiet", MIB)],
    )
    assert partial_empty.status == "partial"
    assert partial_empty.entries == []
    assert "incomplete" in partial_empty.detail.lower()

    partial_rows = _insight(
        policy,
        status="partial",
        rows=[_dir("/var", 14 * GIB)],
    )
    assert partial_rows.status == "partial"
    assert [entry.path for entry in partial_rows.entries] == ["/var"]
    assert "incomplete" in partial_rows.detail.lower()

    complete_with_errors = _insight(
        policy,
        error_count=1,
        rows=[_dir("/quiet", MIB)],
    )
    assert complete_with_errors.status == "complete"


def test_filter_keeps_top_twenty_deterministically_and_counts_omitted():
    rows = [_dir(f"/d{index:02d}", 10 * GIB) for index in range(25)]
    policy = _policy(cap=0.1, floor=0)
    insight = _insight(policy, used=1000 * GIB, rows=rows)
    assert insight.status == "complete"
    assert len(insight.entries) == 20
    assert insight.omitted == 5
    assert [entry.path for entry in insight.entries] == [
        f"/d{index:02d}" for index in range(20)
    ]
    assert all(entry.share_percent == pytest.approx(1.0) for entry in insight.entries)


def test_filter_sorts_by_share_then_bytes_then_path():
    policy = _policy(cap=0.1, floor=0)
    insight = _insight(
        policy,
        used=100 * GIB,
        rows=[
            _dir("/b", 30 * GIB),
            _dir("/a", 30 * GIB),
            _dir("/c", 10 * GIB),
        ],
    )
    assert [entry.path for entry in insight.entries] == ["/a", "/b", "/c"]
    assert [entry.share_percent for entry in insight.entries] == pytest.approx(
        [30.0, 30.0, 10.0]
    )


def test_filter_output_always_fits_the_state_model():
    policy = _policy(floor=0)
    insight = _insight(
        policy,
        rows=[_dir("/var", 18 * GIB), _dir("/var/log", 14 * GIB)],
    )
    # Round-trip through the same validated model the state writer uses.
    assert isinstance(insight, DiskUsageInsight)
    assert all(isinstance(entry, DiskUsageEntry) for entry in insight.entries)
    assert len(insight.detail) <= 160


# --------------------------------------------------------------------------- #
# Collector coordinator
# --------------------------------------------------------------------------- #


def test_collect_prefers_fresh_used_and_forwards_exact_extra_paths():
    policy = _policy(floor=0, caps={STORE: 20.0})
    raw = _facts(rows=[_dir(STORE, 6 * GIB, depth=5)])
    executor = _FakeExecutor(PrimitiveResult(rc=0, facts=raw))
    insight = collect_lxc_disk_insight(
        executor,
        "135",
        df_stdout=_df_row(100 * GIB // 1024, total_kb=200 * GIB // 1024),
        policy=policy,
    )
    assert executor.calls == [("135", (STORE,))]
    assert insight.used_bytes == 20 * GIB
    assert [entry.path for entry in insight.entries] == [STORE]
    assert insight.entries[0].share_percent == pytest.approx(30.0)


def test_collect_falls_back_to_df_used_only_when_fresh_used_is_missing():
    policy = _policy(floor=0)
    raw = _facts(used=None, rows=[_dir("/var/log", 14 * GIB)], root_allocated=0)
    executor = _FakeExecutor(PrimitiveResult(rc=0, facts=raw))
    insight = collect_lxc_disk_insight(
        executor,
        "135",
        df_stdout=_df_row(20 * GIB // 1024, total_kb=40 * GIB // 1024),
        policy=policy,
    )
    assert insight.used_bytes == 20 * GIB
    assert isinstance(insight, DiskUsageInsight)
    assert [entry.path for entry in insight.entries] == ["/var/log"]
    assert insight.entries[0].share_percent == pytest.approx(70.0)


def test_collect_capacity_changes_do_not_change_shares():
    policy = _policy(floor=0)
    raw = _facts(used=None, rows=[_dir("/var/log", 14 * GIB)], root_allocated=0)
    used_kb = 20 * GIB // 1024
    small = collect_lxc_disk_insight(
        _FakeExecutor(PrimitiveResult(rc=0, facts=raw)),
        "135",
        df_stdout=_df_row(used_kb, total_kb=40 * GIB // 1024),
        policy=policy,
    )
    large = collect_lxc_disk_insight(
        _FakeExecutor(PrimitiveResult(rc=0, facts=raw)),
        "135",
        df_stdout=_df_row(used_kb, total_kb=4000 * GIB // 1024),
        policy=policy,
    )
    assert small.used_bytes == large.used_bytes == 20 * GIB
    assert [
        (entry.path, entry.allocated_bytes, entry.share_percent)
        for entry in small.entries
    ] == [
        (entry.path, entry.allocated_bytes, entry.share_percent)
        for entry in large.entries
    ]


def test_collect_failed_exception_and_malformed_facts_stay_unavailable():
    policy = _policy(floor=0)
    failed = collect_lxc_disk_insight(
        _FakeExecutor(
            PrimitiveResult(
                rc=2,
                failed=True,
                stderr="secret diagnostic that must not leak",
            )
        ),
        "135",
        df_stdout="",
        policy=policy,
    )
    assert failed.status == "unavailable"
    assert failed.entries == []
    assert "secret" not in failed.detail

    raised = collect_lxc_disk_insight(
        _FakeExecutor(error=RuntimeError("boom secret")),
        "135",
        df_stdout="",
        policy=policy,
    )
    assert raised.status == "unavailable"
    assert raised.entries == []
    assert "secret" not in raised.detail

    malformed = collect_lxc_disk_insight(
        _FakeExecutor(
            PrimitiveResult(
                rc=0,
                facts={
                    "status": "complete",
                    "root_used_bytes": 20 * GIB,
                    "directories": "bad",
                },
            )
        ),
        "135",
        df_stdout=_df_row(20 * GIB // 1024),
        policy=policy,
    )
    assert malformed.status == "unavailable"
    assert malformed.entries == []
    assert len(malformed.detail) <= 160


def test_collect_malformed_or_zero_df_fallback_is_unavailable():
    policy = _policy(floor=0)
    raw = _facts(used=None, rows=[_dir("/var/log", 14 * GIB)], root_allocated=0)
    executor = _FakeExecutor(PrimitiveResult(rc=0, facts=raw))
    for df_stdout in ("", "garbage", _df_row(0, total_kb=0)):
        insight = collect_lxc_disk_insight(
            executor, "135", df_stdout=df_stdout, policy=policy
        )
        assert insight.status == "unavailable"
        assert insight.entries == []


def test_collect_uses_sorted_policy_extra_paths():
    caps = {
        "/z/z/z/z/z": 20.0,
        "/a/a/a/a/a": 20.0,
    }
    policy = _policy(floor=0, caps=caps)
    raw = _facts(
        used=100 * GIB,
        rows=[
            _dir("/a/a/a/a/a", 30 * GIB, depth=5),
            _dir("/z/z/z/z/z", 30 * GIB, depth=5),
        ],
    )
    executor = _FakeExecutor(PrimitiveResult(rc=0, facts=raw))
    insight = collect_lxc_disk_insight(
        executor, "135", df_stdout="", policy=policy
    )
    assert executor.calls == [("135", ("/a/a/a/a/a", "/z/z/z/z/z"))]
    assert {entry.path for entry in insight.entries} == set(caps)
