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
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    return None


def _reclaim(tool: str, spec: ToolSpec) -> None:
    """Delete files the way the native tool would (keeping pnpm-referenced)."""
    directory = spec.directory
    if directory is None:
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
    ) -> None:
        self.tools = tools
        self.busy = list(busy)
        self.os_type = os_type
        self.running = running
        self.template = template
        self.applied: List[str] = []
        self.probe_calls = 0

    def _entry(self, tool: str) -> Dict[str, Any]:
        spec = self.tools[tool]
        directory = spec.directory
        exists = spec.exists if spec.exists is not None else bool(directory is not None and directory.exists())
        if spec.allocated is _UNSET:
            allocated = _measure(directory) if exists else 0
        else:
            allocated = spec.allocated
        return {
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

    def _facts(self) -> Dict[str, Any]:
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
            "cache_paths": {tool: self._entry(tool) for tool in self.tools},
            "busy_tools": list(self.busy),
            "binaries": {name: "/usr/bin/" + name for name in REQUIRED_BINARIES},
            "log_access_ready": True,
            "log_access_detail": {"alloy_user": True, "dirs": {}, "files": {}},
            "policy_sha256": {"journald": "", "alloy_env": "", "npm_logrotate": ""},
            "profile_evidence": {"npm": {"detected": False}, "pbs": {"detected": False}},
        }

    def housekeeping_probe(self, lxc_id: str) -> PrimitiveResult:
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
