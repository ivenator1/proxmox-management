"""Behavioural regressions for native-safe cache reclamation.

Each test drives :func:`proxmox_fleet.housekeeping_cache.clean_guest_caches`
against:

* a real temporary cache directory whose allocated blocks are really measured
  (the fake transport deletes files exactly as the native tool would and
  returns an injected exit status), and
* a real SQLite :class:`CheckpointStore` for per-tool cadence.

The assertions are about observable policy, cadence and allocated-size results
-- never about forwarding a command string.
"""
from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from proxmox_fleet import housekeeping
from proxmox_fleet import housekeeping_cache as hc
from proxmox_fleet.housekeeping_checkpoint import CheckpointStore, GuestKey
from proxmox_fleet.housekeeping_io import REQUIRED_BINARIES
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult

KEY = GuestKey("cluster-a", "node-1", "120")
_HOUR_NS = 3_600_000_000_000
_NOW = 1_000_000_000


class _Unset:
    pass


_UNSET = _Unset()

#: Resolved paths assumed for tools a test does not exercise directly.
_ABSENT_RESOLVED = {
    "apt": "/var/cache/apt/archives",
    "yarn": "/usr/local/share/.cache/yarn/v6",
    "npm": "/root/.npm",
    "pnpm": "/root/.local/share/pnpm/store/v11",
    "bun": "/root/.bun/install/cache",
}


@dataclass
class ToolSpec:
    resolved: str
    directory: Optional[Path] = None
    exists: Optional[bool] = None
    root: Optional[str] = None
    source: str = "tool"
    version: Optional[str] = "1.0.0"
    allowlisted: bool = True
    escaped: bool = False
    root_owned: bool = True
    dependency_referenced: bool = False
    allocated: object = _UNSET
    error: Optional[str] = None
    command_rc: int = 0
    prune_keep: tuple = ()
    binary: Optional[str] = "/usr/bin/bun"
    cache_prune_supported: bool = True
    reclaimable: object = _UNSET


def _measure(root: Optional[Path]) -> int:
    if root is None or not root.exists():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_blocks * 512
    return total


def _bun_reclaimable(root: Optional[Path]) -> int:
    """Test-local mirror of the helper's conservative Bun reclaimable total."""
    if root is None or not root.exists():
        return 0
    links = root / "links"
    total = 0
    seen = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        in_links = current == links or links in current.parents
        for name in filenames:
            try:
                st = os.lstat(current / name)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or in_links:
                continue
            identity = (st.st_dev, st.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            total += st.st_blocks * 512
    return total


def _cache_dir(tmp_path: Path, name: str, *, files=("pkg.bin",), size: int = 8192) -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    for fname in files:
        (directory / fname).write_bytes(b"x" * size)
    return directory


def _tool_for_command(command: str) -> Optional[str]:
    if "apt-get clean" in command:
        return "apt"
    if "yarn cache clean" in command:
        return "yarn"
    if "npm cache clean" in command:
        return "npm"
    if "pnpm store prune" in command:
        return "pnpm"
    if (
        command.startswith("LC_ALL=C BUN_INSTALL_CACHE_DIR=")
        and " pm cache prune --max-age 30" in command
    ):
        return "bun"
    return None


def _reclaim(tool: str, spec: ToolSpec) -> None:
    """Delete files the way the native tool would (keeping referenced entries)."""
    directory = spec.directory
    if directory is None:
        return
    if tool == "bun":
        # Native prune preserves the global store's links/ subtree and removes
        # aged entries; external hardlink targets are untouched.
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            if entry.name == "links" or entry.name in spec.prune_keep:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        return
    for path in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not path.is_file():
            continue
        if tool == "pnpm" and spec.prune_keep and path.name in spec.prune_keep:
            continue
        path.unlink()


class FakeCacheExecutor:
    """Cache transport backed by real temp directories and injected exit codes."""

    host = "pve"

    def __init__(
        self,
        *,
        tools: Dict[str, ToolSpec],
        busy: Any = (),
        os_type: str = "debian",
        running: bool = True,
        template: bool = False,
        fresh_tools: Optional[Dict[str, ToolSpec]] = None,
    ) -> None:
        self.tools = tools
        self.fresh_tools = fresh_tools
        self.busy = list(busy)
        self.os_type = os_type
        self.running = running
        self.template = template
        self.applied: List[str] = []
        self.probe_calls = 0

    def _entry(self, tool: str, tools: Dict[str, ToolSpec]) -> Dict[str, Any]:
        spec = tools[tool]
        directory = spec.directory
        exists = spec.exists if spec.exists is not None else bool(directory is not None and directory.exists())
        if spec.allocated is _UNSET:
            allocated = _measure(directory) if exists else 0
        else:
            allocated = spec.allocated
        entry = {
            "tool": tool,
            "root": spec.root if spec.root is not None else spec.resolved,
            "resolved": spec.resolved,
            "source": spec.source,
            "exists": exists,
            "allowlisted": spec.allowlisted,
            "escaped": spec.escaped,
            "allocated_bytes": allocated,
            "version": spec.version,
            "dependency_referenced": spec.dependency_referenced,
            "error": spec.error,
            "root_owned": spec.root_owned,
        }
        if tool == "bun":
            entry["binary"] = spec.binary
            entry["cache_prune_supported"] = spec.cache_prune_supported
            if spec.reclaimable is _UNSET:
                entry["reclaimable_bytes"] = _bun_reclaimable(directory) if exists else 0
            else:
                entry["reclaimable_bytes"] = spec.reclaimable
        return entry

    def _facts(self) -> Dict[str, Any]:
        # The first call is the caller's initial probe; every later call is the
        # policy's fresh before/after probe.
        tools = self.fresh_tools if (self.fresh_tools is not None and self.probe_calls >= 2) else self.tools
        return {
            "guest": {
                "name": "guest",
                "os_type": self.os_type,
                "is_running": self.running,
                "is_template": self.template,
            },
            "disk": {"total_bytes": 1000, "available_bytes": 500, "used_percent": 50.0},
            "journal_bytes": 0,
            "profiles": [],
            "files": [],
            "cache_paths": {tool: self._entry(tool, tools) for tool in tools},
            "busy_tools": list(self.busy),
            "binaries": {name: "/usr/bin/" + name for name in REQUIRED_BINARIES},
            "log_access_ready": True,
            "log_access_detail": {"alloy_user": True, "dirs": {}, "files": {}},
            "policy_sha256": {"journald": "", "alloy_env": "", "npm_logrotate": ""},
            "profile_evidence": {"npm": {"detected": False}, "pbs": {"detected": False}},
            "logging": {"plugins": {}, "sources": {}},
        }

    def housekeeping_probe(
        self,
        lxc_id: str,
        *,
        logging_request: Optional[Dict[str, Any]] = None,
    ) -> PrimitiveResult:
        self.probe_calls += 1
        return PrimitiveResult(rc=0, changed=False, failed=False, facts=self._facts())

    def housekeeping_apply(self, lxc_id: str, *, command: str) -> PrimitiveResult:
        self.applied.append(command)
        tool = _tool_for_command(command)
        spec = self.tools.get(tool) if tool else None
        rc = spec.command_rc if spec is not None else 0
        if rc == 0 and spec is not None:
            _reclaim(tool, spec)
        return PrimitiveResult(
            rc=rc,
            changed=rc == 0,
            failed=rc != 0,
            stderr=("native failure" if rc != 0 else ""),
            facts={},
        )

    def housekeeping_log_acl(
        self,
        lxc_id: str,
        *,
        roots: Sequence[str] = (),
        exact_files: Sequence[str] = (),
    ) -> PrimitiveResult:
        changed = bool(tuple(roots) or tuple(exact_files))
        return PrimitiveResult(
            rc=0,
            changed=changed,
            facts={
                "status": "complete",
                "changed": changed,
                "repaired_roots": len(tuple(roots)),
                "repaired_files": len(tuple(exact_files)),
                "skipped_missing": 0,
                "error_count": 0,
                "reason": "",
            },
        )


def _absent(tool: str) -> ToolSpec:
    return ToolSpec(
        resolved=_ABSENT_RESOLVED[tool],
        directory=None,
        exists=False,
        source=("allowlist" if tool == "apt" else "tool"),
        version=(None if tool == "apt" else "1.0.0"),
    )


def _specs(tmp_path: Path, **tools: ToolSpec) -> Dict[str, ToolSpec]:
    return {tool: tools.get(tool, _absent(tool)) for tool in hc.CACHE_TOOLS}


@pytest.fixture
def store(tmp_path: Path):
    handle = CheckpointStore.open(str(tmp_path / "housekeeping.sqlite3"))
    try:
        yield handle
    finally:
        handle.close()


def _run(executor, store, *, dry_run=False, clock_ns=lambda: _NOW, settings=None):
    probe = housekeeping.probe_housekeeping(executor, KEY.lxc_id)
    return hc.clean_guest_caches(
        executor,
        settings or GlobalSettings(),
        store,
        KEY,
        probe,
        dry_run=dry_run,
        clock_ns=clock_ns,
    )


# --------------------------------------------------------------------------- #
# eligible reclamation
# --------------------------------------------------------------------------- #


def test_apt_clean_reclaims_archives_when_due(tmp_path, store):
    directory = _cache_dir(tmp_path, "apt")
    before = _measure(directory)
    assert before > 0
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            apt=ToolSpec(resolved="/var/cache/apt/archives", directory=directory, source="allowlist", version=None),
        )
    )

    result = _run(executor, store)

    assert executor.applied == [hc.CACHE_CLEAN_COMMANDS["apt"]]
    assert result.changed is True
    assert result.failed is False
    assert result.bytes_reclaimed == before
    assert _measure(directory) == 0
    assert store.last_cache_clean(KEY, "apt") == _NOW


def test_npm_yarn_v6_root_reclaims_and_records_cadence(tmp_path, store):
    directory = _cache_dir(tmp_path, "yarn", files=("npm-foo-v1.tgz", "npm-bar-v2.tgz"))
    before = _measure(directory)
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            yarn=ToolSpec(
                resolved="/usr/local/share/.cache/yarn/v6",
                directory=directory,
                version="1.22.19",
            ),
        )
    )

    result = _run(executor, store)

    assert executor.applied == [hc.CACHE_CLEAN_COMMANDS["yarn"]]
    assert result.bytes_reclaimed == before
    assert result.changed is True
    assert _measure(directory) == 0
    assert store.last_cache_clean(KEY, "yarn") == _NOW


def test_homepage_pnpm_store_prune_keeps_referenced_entries(tmp_path, store):
    directory = _cache_dir(tmp_path, "pnpm", files=("pkg-a", "pkg-b", "orphan-c"))
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            pnpm=ToolSpec(
                resolved="/root/.local/share/pnpm/store/v11",
                directory=directory,
                version="9.15.0",
                prune_keep=("pkg-a", "pkg-b"),
            ),
        )
    )

    result = _run(executor, store)

    assert executor.applied == [hc.CACHE_CLEAN_COMMANDS["pnpm"]]
    assert result.changed is True
    assert result.bytes_reclaimed > 0
    assert (directory / "pkg-a").exists()
    assert (directory / "pkg-b").exists()
    assert not (directory / "orphan-c").exists()
    assert store.last_cache_clean(KEY, "pnpm") == _NOW


def test_pnpm_prune_with_nothing_unreferenced_advances_cadence_without_change(tmp_path, store):
    directory = _cache_dir(tmp_path, "pnpm", files=("pkg-a", "pkg-b"))
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            pnpm=ToolSpec(
                resolved="/root/.local/share/pnpm/store/v11",
                directory=directory,
                version="9.15.0",
                prune_keep=("pkg-a", "pkg-b"),
            ),
        )
    )

    result = _run(executor, store)

    assert executor.applied == [hc.CACHE_CLEAN_COMMANDS["pnpm"]]
    assert result.bytes_reclaimed == 0
    assert result.changed is False
    # A successful command advances the cadence even with zero reclamation.
    assert store.last_cache_clean(KEY, "pnpm") == _NOW


# --------------------------------------------------------------------------- #
# busy skips
# --------------------------------------------------------------------------- #


def test_busy_dpkg_lock_skips_apt_without_action(tmp_path, store):
    directory = _cache_dir(tmp_path, "apt")
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            apt=ToolSpec(resolved="/var/cache/apt/archives", directory=directory, source="allowlist", version=None),
        ),
        busy=["dpkg"],
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert result.bytes_reclaimed == 0
    assert result.changed is False
    assert result.failed is False
    assert any("apt" in warning and "activity" in warning for warning in result.warnings)
    assert _measure(directory) > 0
    assert store.last_cache_clean(KEY, "apt") is None


def test_build_activity_blocks_every_tool(tmp_path, store):
    directory = _cache_dir(tmp_path, "yarn")
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            yarn=ToolSpec(resolved="/usr/local/share/.cache/yarn/v6", directory=directory),
        ),
        busy=["build"],
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert result.changed is False
    assert any("build" in warning for warning in result.warnings)
    assert _measure(directory) > 0
    assert store.last_cache_clean(KEY, "yarn") is None


# --------------------------------------------------------------------------- #
# unsafe roots / unsupported tools preserve the cache
# --------------------------------------------------------------------------- #


def test_wrong_yarn_major_is_skipped(tmp_path, store):
    directory = _cache_dir(tmp_path, "yarn")
    before = _measure(directory)
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            yarn=ToolSpec(
                resolved="/usr/local/share/.cache/yarn/v6",
                directory=directory,
                version="4.1.0",
            ),
        )
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert result.bytes_reclaimed == 0
    assert result.changed is False
    assert any("yarn" in warning.lower() and "unsupported" in warning.lower() for warning in result.warnings)
    assert _measure(directory) == before
    assert store.last_cache_clean(KEY, "yarn") is None


def test_arbitrary_deeper_root_is_preserved_even_if_reported_allowlisted(tmp_path, store):
    directory = _cache_dir(tmp_path, "yarn")
    before = _measure(directory)
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            yarn=ToolSpec(
                resolved="/usr/local/share/.cache/yarn/v6/nested",
                directory=directory,
                version="1.22.19",
                allowlisted=True,
            ),
        )
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert any("allowlisted" in warning for warning in result.warnings)
    assert _measure(directory) == before
    assert store.last_cache_clean(KEY, "yarn") is None


def test_symlink_escape_is_preserved_even_if_reported_allowlisted(tmp_path, store):
    directory = _cache_dir(tmp_path, "yarn")
    before = _measure(directory)
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            yarn=ToolSpec(
                resolved="/usr/local/share/.cache/yarn/v6",
                directory=directory,
                version="1.22.19",
                escaped=True,
                allowlisted=True,
            ),
        )
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert any("symlink" in warning for warning in result.warnings)
    assert _measure(directory) == before
    assert store.last_cache_clean(KEY, "yarn") is None


def test_root_not_owned_is_preserved_even_if_reported_allowlisted(tmp_path, store):
    directory = _cache_dir(tmp_path, "npm")
    before = _measure(directory)
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            npm=ToolSpec(resolved="/root/.npm", directory=directory, root_owned=False, allowlisted=True),
        )
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert any("root-owned" in warning for warning in result.warnings)
    assert _measure(directory) == before
    assert store.last_cache_clean(KEY, "npm") is None


def test_dependency_referenced_cache_is_preserved_even_if_reported_allowlisted(tmp_path, store):
    directory = _cache_dir(tmp_path, "pnpm", files=("pkg-a",))
    before = _measure(directory)
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            pnpm=ToolSpec(
                resolved="/root/.local/share/pnpm/store/v11",
                directory=directory,
                version="9.15.0",
                dependency_referenced=True,
                allowlisted=True,
            ),
        )
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert any("dependencies" in warning for warning in result.warnings)
    assert _measure(directory) == before
    assert store.last_cache_clean(KEY, "pnpm") is None


def test_unresolved_native_root_is_skipped(tmp_path, store):
    directory = _cache_dir(tmp_path, "yarn")
    before = _measure(directory)
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            yarn=ToolSpec(
                resolved="/usr/local/share/.cache/yarn/v6",
                directory=directory,
                version="1.22.19",
                source="allowlist",
            ),
        )
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert any("native cache directory" in warning for warning in result.warnings)
    assert _measure(directory) == before
    assert store.last_cache_clean(KEY, "yarn") is None


# --------------------------------------------------------------------------- #
# failures and unmeasurable sizes
# --------------------------------------------------------------------------- #


def test_failed_command_reports_failure_and_retains_cadence(tmp_path, store):
    directory = _cache_dir(tmp_path, "yarn")
    before = _measure(directory)
    spec = ToolSpec(
        resolved="/usr/local/share/.cache/yarn/v6",
        directory=directory,
        version="1.22.19",
        command_rc=1,
    )
    executor = FakeCacheExecutor(tools=_specs(tmp_path, yarn=spec))

    result = _run(executor, store)

    assert executor.applied == [hc.CACHE_CLEAN_COMMANDS["yarn"]]
    assert result.failed is True
    assert result.bytes_reclaimed == 0
    assert any("yarn" in warning and "failed" in warning for warning in result.warnings)
    assert _measure(directory) == before
    assert store.last_cache_clean(KEY, "yarn") is None

    # A later successful run still cleans: the failure did not advance cadence.
    spec.command_rc = 0
    second = _run(executor, store)
    assert second.failed is False
    assert second.bytes_reclaimed == before
    assert store.last_cache_clean(KEY, "yarn") == _NOW


def test_unknown_cache_size_warns_instead_of_claiming_zero(tmp_path, store):
    directory = _cache_dir(tmp_path, "yarn")
    before = _measure(directory)
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            yarn=ToolSpec(
                resolved="/usr/local/share/.cache/yarn/v6",
                directory=directory,
                version="1.22.19",
                allocated=None,
            ),
        )
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert result.bytes_reclaimed == 0
    assert result.changed is False
    assert any("could not be measured" in warning for warning in result.warnings)
    assert _measure(directory) == before
    assert store.last_cache_clean(KEY, "yarn") is None


# --------------------------------------------------------------------------- #
# cadence and idle
# --------------------------------------------------------------------------- #


def test_repeat_success_within_24h_is_suppressed(tmp_path, store):
    directory = _cache_dir(tmp_path, "yarn")
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            yarn=ToolSpec(resolved="/usr/local/share/.cache/yarn/v6", directory=directory, version="1.22.19"),
        )
    )

    first = _run(executor, store, clock_ns=lambda: _NOW)
    assert first.changed is True
    assert len(executor.applied) == 1

    # Refill the cache; a within-window run must still be suppressed by cadence.
    (directory / "fresh.tgz").write_bytes(b"y" * 8192)
    second = _run(executor, store, clock_ns=lambda: _NOW + 23 * _HOUR_NS)
    assert second.changed is False
    assert second.bytes_reclaimed == 0
    assert len(executor.applied) == 1
    assert (directory / "fresh.tgz").exists()

    third = _run(executor, store, clock_ns=lambda: _NOW + 25 * _HOUR_NS)
    assert third.changed is True
    assert len(executor.applied) == 2
    assert _measure(directory) == 0


def test_zero_and_absent_roots_are_idle_without_records(tmp_path, store):
    empty = tmp_path / "npm"
    empty.mkdir(parents=True, exist_ok=True)
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            npm=ToolSpec(resolved="/root/.npm", directory=empty),
            pnpm=ToolSpec(resolved="/root/.local/share/pnpm/store/v11", directory=None, exists=False),
        )
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert result.changed is False
    assert result.bytes_reclaimed == 0
    assert result.warnings == []
    assert store.cache_clean_times(KEY) == {}


# --------------------------------------------------------------------------- #
# dry-run audit
# --------------------------------------------------------------------------- #


def test_dry_run_audits_due_tools_without_any_write(tmp_path):
    db = tmp_path / "housekeeping.sqlite3"
    with CheckpointStore.open(str(db)) as writable:
        writable.record_cache_clean(KEY, "apt", ts_ns=_NOW)

    directory = _cache_dir(tmp_path, "yarn")
    before = _measure(directory)
    apt_dir = _cache_dir(tmp_path, "apt")
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            apt=ToolSpec(resolved="/var/cache/apt/archives", directory=apt_dir, source="allowlist", version=None),
            yarn=ToolSpec(resolved="/usr/local/share/.cache/yarn/v6", directory=directory, version="1.22.19"),
        )
    )

    read_only = CheckpointStore.open(str(db), read_only=True)
    try:
        result = _run(executor, read_only, dry_run=True, clock_ns=lambda: _NOW + 1 * _HOUR_NS)
    finally:
        read_only.close()

    assert executor.applied == []
    assert result.findings is True
    assert result.changed is False
    assert result.bytes_reclaimed == 0
    assert any("yarn" in warning and "due" in warning for warning in result.warnings)
    assert not any(warning.startswith("apt: cache cleanup is due") for warning in result.warnings)
    assert _measure(directory) == before
    assert _measure(apt_dir) > 0


def test_dry_run_with_no_eligible_cache_has_no_findings(tmp_path, store):
    empty = tmp_path / "yarn"
    empty.mkdir(parents=True, exist_ok=True)
    executor = FakeCacheExecutor(
        tools=_specs(tmp_path, yarn=ToolSpec(resolved="/usr/local/share/.cache/yarn/v6", directory=empty))
    )

    result = _run(executor, store, dry_run=True)

    assert executor.applied == []
    assert result.findings is False
    assert result.changed is False


# --------------------------------------------------------------------------- #
# guest state
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        ({"running": False}, "stopped"),
        ({"template": True}, "template"),
        ({"os_type": "alpine"}, "Debian"),
    ],
)
def test_ineligible_guest_state_never_mutates(tmp_path, store, kwargs, needle):
    directory = _cache_dir(tmp_path, "yarn")
    before = _measure(directory)
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            yarn=ToolSpec(resolved="/usr/local/share/.cache/yarn/v6", directory=directory, version="1.22.19"),
        ),
        **kwargs,
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert result.changed is False
    assert result.bytes_reclaimed == 0
    assert any(needle in warning for warning in result.warnings)
    assert _measure(directory) == before
    assert store.last_cache_clean(KEY, "yarn") is None


# --------------------------------------------------------------------------- #
# Bun: native 30-day prune, links/ preserved, conservative accounting
# --------------------------------------------------------------------------- #


def _bun_cache(tmp_path: Path, name: str = "bun") -> Path:
    root = tmp_path / name
    (root / "links").mkdir(parents=True)
    return root


def _bun_file(root: Path, rel: str, size: int = 8192) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _bun_spec(root: Optional[Path], **overrides: Any) -> ToolSpec:
    values: Dict[str, Any] = {
        "resolved": hc.BUN_CACHE_ROOT,
        "directory": root,
        "version": "1.4.4",
    }
    values.update(overrides)
    return ToolSpec(**values)


def test_bun_due_prune_reclaims_aged_entries_and_records_success(tmp_path, store):
    root = _bun_cache(tmp_path)
    old = _bun_file(root, "pkg-old/index.js")
    old_bytes = os.lstat(old).st_blocks * 512
    fresh = _bun_file(root, "fresh-pkg/index.js", size=4096)
    (root / "links" / "dep").symlink_to(root / "fresh-pkg")
    before_reclaim = _bun_reclaimable(root)
    assert before_reclaim > old_bytes  # the retained fresh package is included

    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            bun=_bun_spec(root, prune_keep=("fresh-pkg",)),
        )
    )

    result = _run(executor, store)

    assert executor.applied == [hc.bun_clean_command("/usr/bin/bun")]
    assert result.failed is False
    assert result.changed is True
    assert not old.parent.exists()
    assert fresh.exists()
    # links/ is the retained global-store subtree: native prune never removes it.
    assert (root / "links" / "dep").is_symlink()
    assert (root / "links" / "dep").resolve() == (root / "fresh-pkg").resolve()
    after_reclaim = _bun_reclaimable(root)
    assert result.bytes_reclaimed == before_reclaim - after_reclaim
    assert result.bytes_reclaimed == old_bytes
    assert store.last_cache_clean(KEY, "bun") == _NOW

    # A successful prune advances the cadence: a within-window refill is untouched.
    refill = _bun_file(root, "pkg-old-again/index.js")
    second = _run(executor, store, clock_ns=lambda: _NOW + 23 * _HOUR_NS)
    assert second.changed is False
    assert second.bytes_reclaimed == 0
    assert len(executor.applied) == 1
    assert refill.exists()


def test_bun_successful_zero_reclaim_still_advances_cadence(tmp_path, store):
    root = _bun_cache(tmp_path)
    kept = _bun_file(root, "links/keep/index.js", size=16384)
    before = _measure(root)
    assert before > 0
    assert _bun_reclaimable(root) == 0

    executor = FakeCacheExecutor(tools=_specs(tmp_path, bun=_bun_spec(root)))
    result = _run(executor, store)

    assert executor.applied == [hc.bun_clean_command("/usr/bin/bun")]
    assert result.failed is False
    assert result.changed is False
    assert result.bytes_reclaimed == 0
    assert kept.exists()
    assert _measure(root) == before
    assert store.last_cache_clean(KEY, "bun") == _NOW


def test_bun_failed_native_prune_preserves_cache_and_cadence(tmp_path, store):
    root = _bun_cache(tmp_path)
    old = _bun_file(root, "pkg-old/index.js")
    before_reclaim = _bun_reclaimable(root)
    executor = FakeCacheExecutor(
        tools=_specs(tmp_path, bun=_bun_spec(root, command_rc=1))
    )

    result = _run(executor, store)

    assert executor.applied == [hc.bun_clean_command("/usr/bin/bun")]
    assert result.failed is True
    assert result.bytes_reclaimed == 0
    assert any("bun" in warning and "failed" in warning for warning in result.warnings)
    assert old.exists()
    assert _bun_reclaimable(root) == before_reclaim
    assert store.last_cache_clean(KEY, "bun") is None


def test_bun_unsupported_native_prune_is_preserved(tmp_path, store):
    root = _bun_cache(tmp_path)
    old = _bun_file(root, "pkg-old/index.js")
    executor = FakeCacheExecutor(
        tools=_specs(
            tmp_path, bun=_bun_spec(root, cache_prune_supported=False)
        )
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert result.failed is False
    assert any("unsupported" in warning for warning in result.warnings)
    assert old.exists()
    assert store.last_cache_clean(KEY, "bun") is None


def test_bun_unmeasured_reclaimable_bytes_skip_action(tmp_path, store):
    root = _bun_cache(tmp_path)
    old = _bun_file(root, "pkg-old/index.js")
    executor = FakeCacheExecutor(
        tools=_specs(tmp_path, bun=_bun_spec(root, reclaimable=None))
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert any("could not be measured" in warning for warning in result.warnings)
    assert old.exists()
    assert store.last_cache_clean(KEY, "bun") is None


def test_bun_malformed_reclaimable_facts_fail_closed(tmp_path, store):
    root = _bun_cache(tmp_path)
    old = _bun_file(root, "pkg-old/index.js")
    executor = FakeCacheExecutor(
        tools=_specs(tmp_path, bun=_bun_spec(root, reclaimable="lots"))
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert any("malformed cache facts" in warning for warning in result.warnings)
    assert old.exists()
    assert store.last_cache_clean(KEY, "bun") is None


@pytest.mark.parametrize(
    "busy,needle",
    [
        (["bun"], "activity"),
        (["build"], "build"),
    ],
)
def test_bun_package_or_build_activity_skips_without_action(
    tmp_path, store, busy, needle
):
    root = _bun_cache(tmp_path)
    old = _bun_file(root, "pkg-old/index.js")
    executor = FakeCacheExecutor(
        tools=_specs(tmp_path, bun=_bun_spec(root)), busy=busy
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert any(needle in warning for warning in result.warnings)
    assert old.exists()
    assert store.last_cache_clean(KEY, "bun") is None


@pytest.mark.parametrize(
    "overrides,needle",
    [
        ({"resolved": hc.BUN_CACHE_ROOT + "/nested"}, "allowlisted"),
        ({"escaped": True}, "symlink"),
        ({"root_owned": False}, "root-owned"),
        ({"dependency_referenced": True}, "dependencies"),
        ({"source": "allowlist"}, "native cache directory"),
        ({"binary": "/tmp/bun"}, "fixed system binary"),
        ({"binary": None}, "fixed system binary"),
    ],
)
def test_bun_unsafe_facts_preserve_cache(tmp_path, store, overrides, needle):
    root = _bun_cache(tmp_path)
    old = _bun_file(root, "pkg-old/index.js")
    executor = FakeCacheExecutor(
        tools=_specs(tmp_path, bun=_bun_spec(root, **overrides))
    )

    result = _run(executor, store)

    assert executor.applied == []
    assert any(needle in warning for warning in result.warnings)
    assert old.exists()
    assert store.last_cache_clean(KEY, "bun") is None


@pytest.mark.parametrize(
    "fresh_overrides,needle",
    [
        ({"binary": "/usr/bin/bun"}, "changed before cleanup"),
        ({"resolved": hc.BUN_CACHE_ROOT + "/nested"}, "aborted"),
        ({"cache_prune_supported": False}, "unsupported"),
    ],
)
def test_bun_change_between_initial_and_fresh_probe_aborts(
    tmp_path, store, fresh_overrides, needle
):
    root = _bun_cache(tmp_path)
    old = _bun_file(root, "pkg-old/index.js")
    initial = _specs(
        tmp_path, bun=_bun_spec(root, binary="/usr/local/bin/bun")
    )
    overrides = {"binary": "/usr/local/bin/bun", **fresh_overrides}
    fresh = _specs(tmp_path, bun=_bun_spec(root, **overrides))
    executor = FakeCacheExecutor(tools=initial, fresh_tools=fresh)

    result = _run(executor, store)

    assert executor.applied == []
    assert any(needle in warning for warning in result.warnings)
    assert old.exists()
    assert store.last_cache_clean(KEY, "bun") is None


def test_bun_externally_hardlinked_bytes_are_not_reported_reclaimed(tmp_path, store):
    root = _bun_cache(tmp_path)
    hardlinked = _bun_file(root, "pkg-hard/blob.bin")
    outside = tmp_path / "outside-blob.bin"
    os.link(hardlinked, outside)
    normal = _bun_file(root, "pkg-normal/blob.bin")
    normal_bytes = os.lstat(normal).st_blocks * 512
    assert _bun_reclaimable(root) == normal_bytes  # hardlinked bytes are not freeable

    executor = FakeCacheExecutor(tools=_specs(tmp_path, bun=_bun_spec(root)))
    result = _run(executor, store)

    assert executor.applied == [hc.bun_clean_command("/usr/bin/bun")]
    assert outside.exists() and outside.read_bytes() == b"x" * 8192
    assert result.bytes_reclaimed == normal_bytes
    assert _bun_reclaimable(root) == 0
    assert store.last_cache_clean(KEY, "bun") == _NOW


def test_bun_dry_run_reports_conservative_reclaimable_value(tmp_path, store):
    root = _bun_cache(tmp_path)
    _bun_file(root, "links/keep.bin", size=16384)
    old = _bun_file(root, "pkg-old/blob.bin")
    allocated = _measure(root)
    reclaimable = _bun_reclaimable(root)
    assert 0 < reclaimable < allocated

    executor = FakeCacheExecutor(tools=_specs(tmp_path, bun=_bun_spec(root)))
    result = _run(executor, store, dry_run=True)

    assert executor.applied == []
    assert result.findings is True
    assert result.changed is False
    assert result.bytes_reclaimed == 0
    assert any(
        warning.startswith("bun:") and f"up to {reclaimable} " in warning
        for warning in result.warnings
    )
    assert not any(f"up to {allocated} " in warning for warning in result.warnings)
    assert old.exists()
    assert store.cache_clean_times(KEY) == {}


def test_bun_dry_run_with_only_retained_bytes_has_no_finding(tmp_path, store):
    root = _bun_cache(tmp_path)
    _bun_file(root, "links/keep.bin", size=16384)
    assert _measure(root) > 0
    assert _bun_reclaimable(root) == 0

    executor = FakeCacheExecutor(tools=_specs(tmp_path, bun=_bun_spec(root)))
    result = _run(executor, store, dry_run=True)

    assert executor.applied == []
    assert result.findings is False
    assert result.warnings == []


def test_bun_empty_or_absent_cache_stays_idle(tmp_path, store):
    empty = tmp_path / "bun-empty"
    empty.mkdir()
    executor = FakeCacheExecutor(tools=_specs(tmp_path, bun=_bun_spec(empty)))
    result = _run(executor, store)
    assert executor.applied == []
    assert result.warnings == []
    assert store.cache_clean_times(KEY) == {}

    absent = FakeCacheExecutor(
        tools=_specs(
            tmp_path,
            bun=ToolSpec(
                resolved=hc.BUN_CACHE_ROOT,
                directory=None,
                exists=False,
                source="tool",
                version="1.4.4",
            ),
        )
    )
    result = _run(absent, store)
    assert absent.applied == []
    assert result.warnings == []
    assert store.cache_clean_times(KEY) == {}
