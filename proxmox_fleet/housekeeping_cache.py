"""Short-retention cache cleanup for managed LXC guests (the cache slice).

This module owns *only* cache housekeeping: bounded, native-tool cache
reclamation for the four fixed profiles (APT, Yarn 1, npm, pnpm).  It is
deliberately independent of Loki readiness, import/backfill state and Alloy
delivery: cache space is local, so a central-store outage must never prevent
reclaiming it.  It never deletes dependency trees, build output, databases,
browser caches or PBS datastores, and it never runs autoremove.

The node-side helper already resolved each tool's native cache directory and
reported whether the path is allowlisted, root-owned, symlink-escaped or
referenced by installed dependencies.  This module *re-derives* those safety
facts from the reported resolved path (never trusting a lone ``allowlisted``
boolean) and refuses to act unless every one holds, then re-probes immediately
before and after the native command to measure the real allocated-block change.

Cadence is the trusted per-guest/per-tool checkpoint: a *successful* native
command advances it (even when the measured reclamation is zero), while a busy
skip or a failed command never does.  Missing cache directories are idle and an
empty root does not create a no-op record.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from proxmox_fleet.housekeeping import (
    HousekeepingError,
    HousekeepingProbe,
    probe_housekeeping,
)

if TYPE_CHECKING:  # avoid runtime cycles; used for typing only
    from proxmox_fleet.executor import Executor
    from proxmox_fleet.housekeeping_checkpoint import CheckpointStore, GuestKey
    from proxmox_fleet.models.settings import GlobalSettings


# --------------------------------------------------------------------------- #
# Fixed policy
# --------------------------------------------------------------------------- #

#: Tools cleaned, in a stable order.
CACHE_TOOLS: Tuple[str, ...] = ("apt", "yarn", "npm", "pnpm")

#: The exact native command per tool.  No arbitrary paths or shell fragments.
CACHE_CLEAN_COMMANDS: Dict[str, str] = {
    "apt": "LC_ALL=C apt-get clean",
    "yarn": "yarn cache clean",
    "npm": "npm cache clean --force",
    "pnpm": "pnpm store prune",
}

#: Allowlisted native cache roots (mirrors the node helper's CACHE_SPECS).
_ALLOWED_ROOTS: Dict[str, Tuple[str, ...]] = {
    "apt": ("/var/cache/apt/archives",),
    "yarn": ("/usr/local/share/.cache/yarn", "/root/.cache/yarn"),
    "npm": ("/root/.npm",),
    "pnpm": ("/root/.local/share/pnpm/store",),
}

#: An exact immediate version child that is also allowed (never a deeper path).
_ALLOWED_CHILD: Dict[str, "re.Pattern[str]"] = {
    "yarn": re.compile(r"\Av6\Z"),
    "pnpm": re.compile(r"\Av\d+\Z"),
}

#: Tools whose resolved directory must be reported by the tool itself, so the
#: native command and the verified path cannot disagree.
_NATIVE_RESOLVED = frozenset({"yarn", "npm", "pnpm"})

#: Activity that blocks one tool (a "build" entry blocks every tool).
_BUSY_BLOCKS: Dict[str, Tuple[str, ...]] = {
    "apt": ("apt", "dpkg", "unattended-upgrade"),
    "yarn": ("yarn",),
    "npm": ("npm",),
    "pnpm": ("pnpm",),
}

#: Guest OS IDs the fixed apt-based profiles are trusted on.
_SUPPORTED_OS = frozenset({"debian", "ubuntu"})

#: Every key the node helper reports per tool; a truncated entry fails closed.
_REQUIRED_ENTRY_KEYS: Tuple[str, ...] = (
    "root",
    "resolved",
    "source",
    "exists",
    "allowlisted",
    "escaped",
    "allocated_bytes",
    "version",
    "dependency_referenced",
    "error",
)


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class CacheResult:
    """Outcome of one cache-cleanup pass for a single guest.

    ``changed`` is true only for a positive measured reclamation; a successful
    command that reclaimed nothing measurable still advances the cadence but is
    not a change.  ``findings`` is set only by a dry-run audit.
    """

    changed: bool = False
    bytes_reclaimed: int = 0
    warnings: List[str] = field(default_factory=list)
    failed: bool = False
    findings: bool = False


# --------------------------------------------------------------------------- #
# Small validators
# --------------------------------------------------------------------------- #


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _detail(result: Any) -> str:
    detail = str(
        getattr(result, "stderr", "") or getattr(result, "stdout", "") or "command failed"
    ).strip()
    return " ".join(detail.split())[-400:]


def _major(version: str) -> Optional[int]:
    match = re.match(r"\s*(\d+)", version)
    return int(match.group(1)) if match else None


def _root_allowed(tool: str, resolved: str) -> bool:
    """Re-derive allowlisting from the resolved path (never trust the fact)."""
    for root in _ALLOWED_ROOTS[tool]:
        if resolved == root:
            return True
        if resolved.startswith(root + "/"):
            child = resolved[len(root) + 1:]
            pattern = _ALLOWED_CHILD.get(tool)
            if pattern is not None and "/" not in child and pattern.match(child):
                return True
    return False


def _facts_incomplete(entry: Dict[str, Any]) -> Optional[str]:
    """Return a bounded description of missing/malformed facts, or ``None``."""
    missing = [key for key in _REQUIRED_ENTRY_KEYS if key not in entry]
    if entry.get("exists") and "root_owned" not in entry:
        missing.append("root_owned")
    if missing:
        return "incomplete cache facts (missing: " + ", ".join(missing) + ")"
    for key in ("exists", "allowlisted", "escaped", "dependency_referenced"):
        if not isinstance(entry[key], bool):
            return f"malformed cache facts ({key} is not a boolean)"
    if "root_owned" in entry and not isinstance(entry["root_owned"], bool):
        return "malformed cache facts (root_owned is not a boolean)"
    if entry["allocated_bytes"] is not None and not _is_int(entry["allocated_bytes"]):
        return "malformed cache facts (allocated_bytes is not an integer or null)"
    for key in ("root", "resolved", "version", "error"):
        if entry[key] is not None and not isinstance(entry[key], str):
            return f"malformed cache facts ({key} is not a string or null)"
    if entry["source"] not in (None, "tool", "allowlist"):
        return "malformed cache facts (source is not tool/allowlist)"
    return None


def _safety_problem(tool: str, entry: Dict[str, Any]) -> Optional[str]:
    """Describe why this tool's resolved root must not be mutated, or ``None``."""
    if entry.get("escaped"):
        return "cache root resolves outside its recorded path via a symlink"
    resolved = entry.get("resolved")
    if not isinstance(resolved, str) or not resolved.startswith("/"):
        return "cache root could not be resolved to an absolute path"
    if not _root_allowed(tool, resolved):
        return f"resolved cache root {resolved!r} is not an allowlisted cache location"
    if not entry.get("allowlisted"):
        return f"cache root {resolved!r} is not an allowlisted cache location"
    if not entry.get("root_owned"):
        return f"cache root {resolved!r} is not root-owned"
    if entry.get("dependency_referenced"):
        return f"cache root {resolved!r} is referenced by installed dependencies"
    if tool in _NATIVE_RESOLVED and entry.get("source") != "tool":
        return f"{tool} could not report its native cache directory"
    return None


def _version_problem(tool: str, entry: Dict[str, Any]) -> Optional[str]:
    """Only Yarn's major is behaviour-significant: non-1 majors are skipped."""
    if tool != "yarn":
        return None
    version = entry.get("version")
    if not isinstance(version, str) or _major(version) != 1:
        label = version if isinstance(version, str) and version else "unknown"
        return f"unsupported Yarn version {label!r} (only Yarn 1 is supported)"
    return None


def _busy_problem(tool: str, busy: Any) -> Optional[str]:
    if "build" in busy:
        return "a package/build operation is in progress"
    blocked = [name for name in _BUSY_BLOCKS[tool] if name in busy]
    if blocked:
        return f"package activity in progress ({', '.join(blocked)})"
    return None


def _guest_problem(probe: HousekeepingProbe) -> Optional[str]:
    if probe.is_template:
        return "guest is a template"
    if not probe.is_running:
        return "guest is stopped"
    if probe.os_type not in _SUPPORTED_OS:
        return f"guest OS {probe.os_type or 'unknown'!r} is not Debian/apt-based"
    return None


def _tool_entry(source: Any, tool: str) -> Optional[Dict[str, Any]]:
    entry = getattr(source, "cache_paths", {})
    if not isinstance(entry, dict):
        return None
    value = entry.get(tool)
    return value if isinstance(value, dict) else None


def _reprobe(
    executor: "Executor", lxc_id: str, tool: str, phase: str, warnings: List[str]
) -> Optional[HousekeepingProbe]:
    try:
        return probe_housekeeping(executor, lxc_id)
    except HousekeepingError as exc:  # noqa: BLE001 - probe boundary stays best-effort
        warnings.append(
            f"{tool}: could not re-probe the guest {phase} cache cleanup: {exc}"
        )
        return None
    except Exception as exc:  # noqa: BLE001 - a transport failure must not crash the run
        warnings.append(
            f"{tool}: could not re-probe the guest {phase} cache cleanup: {exc}"
        )
        return None


# --------------------------------------------------------------------------- #
# Public policy entrypoint
# --------------------------------------------------------------------------- #


def clean_guest_caches(
    executor: "Executor",
    settings: "GlobalSettings",
    store: "CheckpointStore",
    key: "GuestKey",
    probe: HousekeepingProbe,
    *,
    dry_run: bool = False,
    clock_ns: Callable[[], int] = time.time_ns,
) -> CacheResult:
    """Reclaim allowlisted native caches for one guest, bounded by cadence.

    ``probe`` is the caller's read-only introspection.  Every action re-probes
    immediately beforehand and afterwards; the native command's exit status is
    authoritative and a *successful* command (even with zero measured
    reclamation) advances the per-tool cadence while a busy skip or a failure
    does not.  ``dry_run`` performs a read-only cadence audit with no command and
    no checkpoint write.
    """
    result = CacheResult()
    warnings = result.warnings

    guest_problem = _guest_problem(probe)
    if guest_problem is not None:
        warnings.append(f"cache cleanup skipped: {guest_problem}")
        return result

    lxc_id = key.lxc_id
    interval_ns = int(settings.housekeeping_cache_interval_hours) * 3_600_000_000_000
    now = int(clock_ns())
    busy = {str(name) for name in probe.busy_tools}

    for tool in CACHE_TOOLS:
        entry = _tool_entry(probe, tool)
        if not isinstance(entry, dict):
            warnings.append(f"{tool}: cache facts are missing or malformed; skipping cache cleanup")
            continue
        if not entry.get("exists"):
            # A missing cache directory is idle.
            continue
        last = store.last_cache_clean(key, tool)
        if last is not None and interval_ns > 0 and (now - int(last)) < interval_ns:
            continue  # cleaned within the cadence window; silent

        if dry_run:
            problem = _facts_incomplete(entry) or _safety_problem(tool, entry) or _version_problem(tool, entry)
            if problem is not None:
                warnings.append(f"{tool}: {problem}; would skip cache cleanup")
                continue
            busy_problem = _busy_problem(tool, busy)
            if busy_problem is not None:
                warnings.append(f"{tool}: would skip cache cleanup; {busy_problem}")
                continue
            allocated = entry.get("allocated_bytes")
            if allocated is None:
                warnings.append(f"{tool}: cache size could not be measured; audit is incomplete")
                continue
            if not _is_int(allocated) or allocated <= 0:
                continue  # empty cache is idle
            warnings.append(f"{tool}: cache cleanup is due (up to {allocated} allocated bytes reclaimable)")
            result.findings = True
            continue

        incomplete = _facts_incomplete(entry)
        if incomplete is not None:
            warnings.append(f"{tool}: {incomplete}; skipping cache cleanup")
            continue
        problem = _safety_problem(tool, entry) or _version_problem(tool, entry)
        if problem is not None:
            warnings.append(f"{tool}: {problem}; cache and dependencies preserved")
            continue
        busy_problem = _busy_problem(tool, busy)
        if busy_problem is not None:
            warnings.append(f"{tool}: cache cleanup skipped; {busy_problem}")
            continue
        allocated = entry.get("allocated_bytes")
        if allocated is None:
            warnings.append(f"{tool}: cache size could not be measured; skipping cache cleanup")
            continue
        if not _is_int(allocated) or allocated <= 0:
            continue  # empty cache is idle

        # Re-probe immediately before acting; re-validate the fresh facts and
        # measure the baseline so the reclaim delta reflects reality.
        fresh_probe = _reprobe(executor, lxc_id, tool, "before", warnings)
        if fresh_probe is None:
            continue
        if _guest_problem(fresh_probe) is not None:
            warnings.append(f"{tool}: guest state changed before cleanup; skipping")
            continue
        fresh = _tool_entry(fresh_probe, tool)
        fresh_problem = None
        if fresh is None:
            fresh_problem = "cache facts are missing or malformed"
        else:
            fresh_problem = (
                _facts_incomplete(fresh)
                or _safety_problem(tool, fresh)
                or _version_problem(tool, fresh)
                or _busy_problem(tool, {str(name) for name in fresh_probe.busy_tools})
            )
        if fresh is None or fresh_problem is not None:
            warnings.append(f"{tool}: {fresh_problem}; cache cleanup aborted")
            continue
        before = fresh.get("allocated_bytes")
        if not isinstance(before, int) or isinstance(before, bool):
            warnings.append(f"{tool}: cache size could not be measured before cleanup; aborted")
            continue
        if before <= 0:
            continue  # became idle

        applied = executor.housekeeping_apply(lxc_id, command=CACHE_CLEAN_COMMANDS[tool])
        if getattr(applied, "failed", False) or getattr(applied, "rc", 1) != 0:
            result.failed = True
            warnings.append(f"{tool}: cache cleanup command failed: {_detail(applied)}")
            continue

        # The native command succeeded: advance the cadence even when the
        # reclaim is unmeasurable or zero.
        store.record_cache_clean(key, tool, ts_ns=now)

        after_probe = _reprobe(executor, lxc_id, tool, "after", warnings)
        after_entry = _tool_entry(after_probe, tool) if after_probe is not None else None
        after = after_entry.get("allocated_bytes") if isinstance(after_entry, dict) else None
        if not isinstance(after, int) or isinstance(after, bool):
            warnings.append(
                f"{tool}: cache cleanup succeeded but reclaimed bytes could not be measured"
            )
            continue
        delta = before - after
        if delta > 0:
            result.bytes_reclaimed += delta
            result.changed = True
        elif delta < 0:
            warnings.append(f"{tool}: cache grew by {-delta} bytes during cleanup")

    return result
