#!/usr/bin/env python3
"""Transport helper for the fleet log housekeeping feature (step 2).

This module is a **standalone stdlib-only** program.  The manager stages it on a
Proxmox node and invokes it as::

    python3 housekeeping_io.py --operation probe       --lxc-id 123 --request req.json
    python3 housekeeping_io.py --operation disk_usage  --lxc-id 123 --request req.json
    python3 housekeeping_io.py --operation logging_acl --lxc-id 123 --request req.json
    python3 housekeeping_io.py --operation capture  --lxc-id 123 --request req.json
    python3 housekeeping_io.py --operation snapshot --lxc-id 123 --request req.json \\
            --destination /root/staging/snapshot.tar
    python3 housekeeping_io.py --operation prune    --lxc-id 123 --request req.json

Contract
--------
* stdout always carries exactly **one** compact JSON object (single line).
  On success it is the operation facts object and the exit status is ``0``.
  On failure the exit status is non-zero and the object is
  ``{"error": "<message>", "detail": {...}}``.  Log bodies are never emitted.
* There is **no** age/cadence/HTTP/update policy here: the helper only reads
  guest state, freezes capture prefixes into the node spool, streams bounded
  byte ranges into a tar, and performs identity-checked quarantine deletions.
  ``logging_acl`` is the single mutation exception: it grants the Alloy user a
  scoped POSIX ACL through inherited descriptors on already-resolved catalogue
  paths, never ``chmod``/``chown`` and never a caller-supplied command.

Execution topology
------------------
``capture`` / ``snapshot`` / ``prune`` run on the node; guest ``path`` values
are resolved against a pinned running init process's private mount root (or an
explicit test ``--sysroot``); never guess the node's potentially empty rootfs
mount.  Node ``blob_path`` and ``--destination`` are node-absolute and used
verbatim.  Prune's writer and quarantine-ownership checks read the guest
``/proc`` and root through that pinned root: the node's initial-user-namespace
root can inspect every guest process descriptor, whereas the container's own
root cannot always read a non-dumpable task's ``fd`` table -- so prune is never
delegated to ``pct exec``.  ``probe`` still needs the guest's own ``/proc``
identity, so on a node the helper obtains guest state via read-only ``pct
config``/``pct status`` (never starting the container) and runs its logic inside
the container with ``pct exec <id> -- python3 -`` and the staged helper file on
stdin.  Passing ``--guest-local`` (internal) means "do not orchestrate, run
against ``--sysroot``".

Everything is resolved through a *sysroot* string so the same code paths can be
driven against a temporary fixture tree by the regression tests.

Capture spool layout
--------------------
``capture`` freezes prefixes into ``<spool_root>/<capture_id>/`` as
``<64hex>.blob`` files, one durable fsynced root-only receipt per blob
(``receipts/<64hex>.blob.json``), and a single checksummed ``manifest.json``
written once per call (never after every blob).  The manager writes a cumulative
``released.json`` marker (see `housekeeping_import.build_capture_release_command`)
*before* unlinking acknowledged blobs, so a retried capture never recaptures a
released blob even after the guest original rotated away.  ``snapshot`` validates
a frozen blob against its receipt metadata (device/inode/size/mtime_ns) instead
of re-hashing a multi-GiB blob for every bounded fetch.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fnmatch
import hashlib
import io
import json
import math
import os
import re
import select
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

__all__ = [
    "HelperError",
    "probe",
    "disk_usage",
    "logging_acl",
    "capture",
    "snapshot",
    "prune",
    "main",
    "SPOOL_ROOT",
    "QUARANTINE_DIRNAME",
    "MANIFEST_FILENAME",
    "RECEIPTS_DIRNAME",
    "RELEASED_FILENAME",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Persistent captures require owner-only directories; creation/resume reject
# symlinks, foreign owners and group/other access before opening any blob.
SPOOL_ROOT = "/var/tmp/fleet-log-import"  # nosec B108
QUARANTINE_DIRNAME = ".fleet-housekeeping-quarantine"
FREE_RESERVE_BYTES = 2 * 1024 ** 3
MAX_SNAPSHOT_BYTES = 32 * 1024 ** 2
MAX_REQUEST_BYTES = 64 * 1024 ** 2
MAX_MANIFEST_BYTES = 64 * 1024 ** 2
MAX_ACTIVE_INDEX_BYTES = 256 * 1024

#: Per-capture spool layout inside ``<SPOOL_ROOT>/<capture_id>/``:
#:   manifest.json                - aggregate, checksummed, rewritten once per capture() call
#:   receipts/<64hex>.blob.json   - durable per-blob receipt (metadata + digest), fsynced per blob
#:   released.json                - importer-owned cumulative release marker (written before unlink)
MANIFEST_FILENAME = "manifest.json"
RECEIPTS_DIRNAME = "receipts"
RELEASED_FILENAME = "released.json"
COPY_CHUNK = 1024 * 1024
HASH_CHUNK = 1024 * 1024
EXEC_TIMEOUT = 120.0
INODE_SEARCH_LIMIT = 300_000

CAPTURE_ID_RE = re.compile(r"\A[0-9a-f]{64}/[0-9a-f]{32}\Z")
BLOB_BASENAME_RE = re.compile(r"\A[0-9a-f]{64}\.blob\Z")
HEX2_RE = re.compile(r"\A[0-9A-Fa-f]{2}\Z")
NPM_LOG_RE = re.compile(r"\A.+\.log(?:\.\d+|-\d{8}T\d{6})?(?:\.(?:gz|zst|zstd))?\Z")
NPM_ARCHIVE_RE = re.compile(r"\A.+\.log(?:\.\d+|-\d{8}T\d{6})(?:\.(?:gz|zst|zstd))?\Z")
PBS_API_RE = re.compile(r"\A(?:access|auth)\.log(?:\.\d+)?(?:\.(?:gz|zst|zstd))?\Z")
PBS_API_ARCHIVE_RE = re.compile(r"\A(?:access|auth)\.log\.\d+(?:\.(?:gz|zst|zstd))?\Z")
UPID_RE = re.compile(r"UPID:[^\s\"',\]\}]+")

COMPRESSIONS: Tuple[str, ...] = ("plain", "gzip", "zstd")
PROFILES: Tuple[str, ...] = ("npm", "pbs")
LOG_KINDS: Tuple[str, ...] = ("application", "task", "api", "task_index")

WIRE_KEYS: Tuple[str, ...] = (
    "path",
    "device",
    "inode",
    "size",
    "mtime_ns",
    "allocated_bytes",
    "compression",
    "profile",
    "log_kind",
    "is_active",
    "capture_id",
    "blob_path",
    "offset",
    "length",
)

#: Declarative-plugin fields that must never be accepted by the archive
#: capture/snapshot/prune surfaces.  Logging evidence is a separate read-only
#: payload; widening these closed legacy enums would blur deletion eligibility.
_PLUGIN_RECORD_KEYS: frozenset = frozenset(
    {
        "app",
        "component",
        "kind",
        "layout_id",
        "owner_id",
        "service_id",
        "allowed_roots",
        "alternate_roots",
        "exact_files",
        "paths",
        "exclude_paths",
        "roots",
        "multiline",
        "service_user",
        "journal_units",
        "required",
    }
)
_PLUGIN_REQUEST_KEYS: frozenset = frozenset({"logging", "plugins", "sources"})

#: Guest-visible roots the feature is ever allowed to read from or delete in.
NPM_LOG_ROOT = "/data/logs"
PBS_TASK_ROOT = "/var/log/proxmox-backup/tasks"
PBS_API_ROOT = "/var/log/proxmox-backup/api"
GUEST_LOG_ROOTS: Tuple[str, ...] = (NPM_LOG_ROOT, PBS_TASK_ROOT, PBS_API_ROOT)
NPM_APP_ROOT = "/opt/nginxproxymanager"

NPM_UNITS = ("npm.service", "openresty.service")
PBS_UNITS = ("proxmox-backup-proxy.service",)
UNIT_DIRS = ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system")
BIN_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
JOURNAL_DIRS = ("/var/log/journal", "/run/log/journal")
PBS_CONTROL_NAMES = frozenset({"active", ".active.lock"})
NPM_NATIVE_LOGROTATE = "/etc/logrotate.d/nginx-proxy-manager"

#: Owned policy files whose hashes the policy verifies (empty hash when absent).
POLICY_FILES: Dict[str, str] = {
    "journald": "/etc/systemd/journald.conf.d/60-fleet-retention.conf",
    "alloy_env": "/etc/systemd/system/alloy.service.d/60-fleet-retention.conf",
    "npm_logrotate": "/etc/fleet-update/npm-logrotate.conf",
}

# Kernel-owned roots are pinned once; only this anchor may cross a proc symlink.
_PINNED_GUEST_ROOTS: Dict[str, int] = {}

REQUIRED_BINARIES: Tuple[str, ...] = (
    "alloy",
    "setfacl",
    "getfacl",
    "logrotate",
    "apt-get",
    "journalctl",
    "systemctl",
    "python3",
    "runuser",
    "yarn",
    "npm",
    "pnpm",
    "proxmox-backup-manager",
)

#: Package-manager lock files whose held writable fd marks the tool busy even
#: when the owning process argv is nonstandard (wrapper scripts, python tooling).
#: Detection is read-only lstat + comparison against the /proc writable-fd inode
#: set; the lock files are never created, removed or truncated.
PACKAGE_LOCK_FILES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("/var/lib/dpkg/lock", ("dpkg",)),
    ("/var/lib/dpkg/lock-frontend", ("apt", "dpkg")),
    ("/var/cache/apt/archives/lock", ("apt",)),
    ("/var/lib/apt/lists/lock", ("apt",)),
)

BUSY_PROGRAMS: Dict[str, frozenset] = {
    "apt": frozenset({"apt", "apt-get", "apt-key"}),
    "dpkg": frozenset({"dpkg", "dpkg-deb", "dpkg-preconfigure"}),
    "unattended-upgrade": frozenset({"unattended-upgrade"}),
    "yarn": frozenset({"yarn"}),
    "npm": frozenset({"npm", "npx"}),
    "pnpm": frozenset({"pnpm"}),
    # ``bun`` itself is classified per argv below: a long-running runtime
    # entrypoint must not self-block pruning, while package/build shapes do.
    "bun": frozenset({"bunx"}),
    "build": frozenset(
        {
            "make",
            "gmake",
            "gcc",
            "g++",
            "cc",
            "c++",
            "cmake",
            "ninja",
            "node-gyp",
            "webpack",
            "vite",
            "rollup",
            "tsc",
            "esbuild",
        }
    ),
}


#: Bun's native cache root (exact allowlist; never a custom/global widening).
BUN_CACHE_ROOT = "/root/.bun/install/cache"

#: Bun's bounded, anchored native-help acceptance limit.
MAX_BUN_HELP_BYTES = 16 * 1024

#: Retained global-store subtree that native prune never removes.
_BUN_LINKS_DIRNAME = "links"

#: Physical walk bound for the age-preserving Bun measurement.
_MAX_BUN_WALK_DEPTH = 128

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BUN_ENTRYPOINT_SUFFIXES = (".js", ".ts", ".mjs", ".cjs", ".tsx", ".jsx")

#: Bounded read-only root-filesystem usage diagnostic (never a cleanup grant).
#: Directory aggregates are reported to ``max_depth``; requested deep prefixes
#: are extra counters inside the same single root walk.
DISK_USAGE_MAX_DEPTH = 3
DISK_USAGE_MAX_EXTRA_PATHS = 32
DISK_USAGE_MAX_EXTRA_DEPTH = 16
DISK_USAGE_MAX_PATH_BYTES = 512
DISK_USAGE_MAX_ENTRIES = 200_000
DISK_USAGE_MIN_ENTRIES = 1
DISK_USAGE_MAX_SECONDS = 15.0
DISK_USAGE_MAX_ROWS = 1000
DISK_USAGE_MAX_RESERVED_ROWS = 129
DISK_USAGE_MAX_WIRE_BYTES = 1024 * 1024
DISK_USAGE_PHYSICAL_DEPTH = 128
DISK_USAGE_PROGRESS_SECONDS = 1.0
DISK_USAGE_KILL_GRACE_SECONDS = 0.25
DISK_USAGE_NODE_BUDGET_SECONDS = 20.0
DISK_USAGE_STATE_TIMEOUT_SECONDS = 2.0
DISK_USAGE_TEARDOWN_SECONDS = 0.5
DISK_USAGE_TRANSPORT_RESERVE_SECONDS = 0.75
DISK_USAGE_MIN_WORKER_SECONDS = 1.0
DISK_USAGE_READ_CHUNK = 64 * 1024
DISK_USAGE_DRAIN_SECONDS = 0.15
DISK_USAGE_MAX_REASON_CHARS = 200
DISK_USAGE_MAX_WIRE_PATH_CHARS = 4096
DISK_USAGE_PROGRESS_ROWS = 200
DISK_USAGE_STATUSES: Tuple[str, ...] = ("complete", "partial", "unavailable")


class _CacheSpec:
    __slots__ = ("tool", "roots", "command", "child")

    def __init__(
        self,
        tool: str,
        roots: Sequence[str],
        command: Optional[Sequence[str]],
        child: Optional[re.Pattern],
    ) -> None:
        self.tool = tool
        self.roots = tuple(roots)
        self.command = tuple(command) if command else None
        self.child = child


CACHE_SPECS: Tuple[_CacheSpec, ...] = (
    _CacheSpec("apt", ("/var/cache/apt/archives",), None, None),
    _CacheSpec(
        "yarn",
        ("/usr/local/share/.cache/yarn", "/root/.cache/yarn"),
        ("yarn", "cache", "dir"),
        re.compile(r"\Av6\Z"),
    ),
    _CacheSpec("npm", ("/root/.npm",), ("npm", "config", "get", "cache"), None),
    _CacheSpec(
        "pnpm",
        ("/root/.local/share/pnpm/store",),
        ("pnpm", "store", "path"),
        re.compile(r"\Av\d+\Z"),
    ),
    # Bun's native prune uses max(atime, mtime) per package directory, so its
    # enumeration must never refresh a directory access time (see
    # _bun_cache_usage).  The exact root is the only allowlisted location.
    _CacheSpec("bun", (BUN_CACHE_ROOT,), ("bun", "pm", "cache"), None),
)

#: Dependency roots whose symlinks reveal an in-use cache.
DEPENDENCY_ROOTS = ("/app/node_modules",)


class HelperError(RuntimeError):
    """A deterministic transport failure (non-zero exit, JSON on stdout)."""

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.detail: Dict[str, Any] = dict(detail or {})


class _PruneBlocked(Exception):
    """A per-file environmental refusal during prune.

    Raised when the helper cannot trust the environment -- an unreadable guest
    ``/proc`` writer table, a foreign-owned quarantine directory, a failed
    space measurement.  It is *not* a structural request error: ``prune()``
    converts it into a ``blocked`` wire entry, keeps every already-emitted
    outcome, reports the remaining unattempted files as ``pending`` and stops
    all further deletions.  It is deliberately not a ``HelperError`` so a
    nested ``except HelperError`` cannot swallow and re-label it.
    """


Runner = Callable[[Sequence[str]], Optional[Tuple[int, str, str]]]


# --------------------------------------------------------------------------- #
# Small filesystem / request helpers
# --------------------------------------------------------------------------- #


def _join(sysroot: Optional[str], guest_path: str) -> str:
    """Map an absolute guest path into the active sysroot."""
    if not isinstance(guest_path, str) or not guest_path.startswith("/"):
        raise HelperError("path must be an absolute string", detail={"path": guest_path})
    if _dotdot(guest_path):
        raise HelperError("path must not contain '..' components", detail={"path": guest_path})
    if sysroot is None:
        raise HelperError(
            "a guest sysroot is required to resolve this path",
            detail={"path": guest_path},
        )
    root = sysroot.rstrip("/")
    if root == "":
        return guest_path
    return root + guest_path


def _dotdot(path: str) -> bool:
    return any(part == ".." for part in path.split("/"))


def _guest_rel(sysroot: str, joined: str) -> str:
    root = sysroot.rstrip("/")
    if root == "":
        return joined
    if joined == root:
        return "/"
    if joined.startswith(root + "/"):
        return joined[len(root):]
    raise HelperError("path is outside the sysroot", detail={"path": joined})


def _listdir(path: str) -> Optional[List[str]]:
    try:
        return sorted(os.listdir(path))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HelperError(
            f"failed to enumerate {path}: {exc.strerror or exc}", detail={"path": path}
        )


def _lstat_strict(path: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise HelperError(
            f"failed to stat {path}: {exc.strerror or exc}", detail={"path": path}
        )


def _read_bytes_optional(path: str, limit: int) -> Optional[bytes]:
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        return None
    except OSError as exc:
        raise HelperError(
            f"failed to read {path}: {exc.strerror or exc}", detail={"path": path}
        )
    if len(data) > limit:
        raise HelperError(f"{path} exceeds the {limit} byte read limit", detail={"path": path})
    return data


def _file_sha256_optional(path: str) -> str:
    data = _read_bytes_optional(path, 4 * 1024 * 1024)
    if data is None:
        return ""
    return hashlib.sha256(data).hexdigest()


def _policy_hashes(sysroot: str) -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    for name, path in POLICY_FILES.items():
        resolved = _join(sysroot, path)
        if name == "npm_logrotate":
            try:
                os.lstat(resolved)
            except FileNotFoundError:
                resolved = _join(sysroot, NPM_NATIVE_LOGROTATE)
        hashes[name] = _file_sha256_optional(resolved)
    return hashes


def _walk(root: str) -> Iterator[Tuple[str, List[str], List[str]]]:
    def onerror(exc: OSError) -> None:
        target = getattr(exc, "filename", None) or root
        raise HelperError(
            f"failed to enumerate {target}: {exc.strerror or exc}", detail={"path": target}
        )

    return os.walk(root, onerror=onerror, followlinks=False)


def _tree_allocated(*paths: str) -> int:
    total = 0
    for root in paths:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in _walk(root):
            dirnames[:] = [d for d in dirnames if d != QUARANTINE_DIRNAME]
            for name in filenames:
                st = _lstat_strict(os.path.join(dirpath, name))
                if stat.S_ISREG(st.st_mode):
                    total += st.st_blocks * 512
    return total


def _fs_used_bytes(path: str) -> int:
    st = os.statvfs(path)
    return (st.f_blocks - st.f_bfree) * (st.f_frsize or st.f_bsize)


def _fs_free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * (st.f_frsize or st.f_bsize)


def _nearest_existing(path: str) -> str:
    current = path.rstrip("/") or "/"
    while not os.path.exists(current):
        parent = os.path.dirname(current) or "/"
        if parent == current:
            break
        current = parent
    return current


def _compression_for(name: str) -> str:
    if name.endswith(".gz"):
        return "gzip"
    if name.endswith(".zst") or name.endswith(".zstd"):
        return "zstd"
    return "plain"


def _wire_from_stat(
    st: os.stat_result,
    *,
    path: str,
    compression: str,
    profile: str,
    log_kind: str,
    is_active: bool,
) -> Dict[str, Any]:
    return {
        "path": path,
        "device": int(st.st_dev),
        "inode": int(st.st_ino),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "allocated_bytes": int(st.st_blocks * 512),
        "compression": compression,
        "profile": profile,
        "log_kind": log_kind,
        "is_active": bool(is_active),
    }


def _validate_compression(spec: Dict[str, Any], *, index: int) -> None:
    if spec.get("compression") not in COMPRESSIONS:
        raise HelperError(
            f"request files[{index}].compression must be one of {COMPRESSIONS}",
            detail={"index": index, "path": spec.get("path")},
        )


def _validate_profile(spec: Dict[str, Any], *, index: int) -> None:
    if spec.get("profile") not in PROFILES:
        raise HelperError(
            f"request files[{index}].profile must be one of {PROFILES}",
            detail={"index": index, "path": spec.get("path")},
        )


def _validate_log_kind(spec: Dict[str, Any], *, index: int) -> None:
    if spec.get("log_kind") not in LOG_KINDS:
        raise HelperError(
            f"request files[{index}].log_kind must be one of {LOG_KINDS}",
            detail={"index": index, "path": spec.get("path")},
        )


def _validate_source_provenance(spec: Dict[str, Any], *, index: int) -> None:
    """Cross-check a capture wire record against its own path (fail closed).

    The recorded ``profile``/``log_kind``/``compression`` must be consistent with
    the absolute source path: a worker that mixed up two profiles cannot capture
    the wrong file under the wrong provenance.
    """
    path = spec["path"]
    profile = spec.get("profile")
    log_kind = spec.get("log_kind")
    root = _log_root_for(path)
    if profile == "npm":
        if log_kind != "application" or root != NPM_LOG_ROOT:
            raise HelperError(
                "npm capture sources must be application logs under the NPM log root",
                detail={"index": index, "path": path, "log_kind": log_kind},
            )
    elif profile == "pbs":
        if log_kind in ("task", "task_index"):
            if root != PBS_TASK_ROOT:
                raise HelperError(
                    "PBS task capture sources must live under the task log root",
                    detail={"index": index, "path": path, "log_kind": log_kind},
                )
        elif log_kind == "api":
            if root != PBS_API_ROOT:
                raise HelperError(
                    "PBS API capture sources must live under the API log root",
                    detail={"index": index, "path": path, "log_kind": log_kind},
                )
        else:
            raise HelperError(
                "PBS capture sources must be task, task_index or api logs",
                detail={"index": index, "path": path, "log_kind": log_kind},
            )
    expected = _compression_for(os.path.basename(path))
    if spec.get("compression") != expected:
        raise HelperError(
            "recorded compression does not match the source filename",
            detail={
                "index": index,
                "path": path,
                "compression": spec.get("compression"),
                "expected": expected,
            },
        )


def _int_field(spec: Dict[str, Any], key: str, *, index: int) -> int:
    value = spec.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HelperError(
            f"request files[{index}].{key} must be a non-negative integer",
            detail={"index": index, "path": spec.get("path"), "key": key},
        )
    return value


def _reject_plugin_request(request: Dict[str, Any], *, operation: str) -> None:
    """Archive operations only accept legacy capture/prune wire records."""
    unexpected = sorted(set(request) & _PLUGIN_REQUEST_KEYS)
    if unexpected:
        raise HelperError(
            f"{operation} requests must not carry declarative logging fields",
            detail={"unexpected": unexpected},
        )


def _request_files(
    request: Dict[str, Any], *, require: Sequence[str] = ()
) -> List[Dict[str, Any]]:
    files = request.get("files")
    if files is None:
        files = []
    if not isinstance(files, list):
        raise HelperError("request 'files' must be a list")
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise HelperError(f"request files[{index}] must be an object", detail={"index": index})
        unexpected = sorted(set(item) & _PLUGIN_RECORD_KEYS)
        if unexpected:
            raise HelperError(
                f"request files[{index}] carries declarative logging fields",
                detail={"index": index, "unexpected": unexpected},
            )
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith("/") or _dotdot(path):
            raise HelperError(
                f"request files[{index}].path must be an absolute traversal-free path",
                detail={"index": index},
            )
        for key in require:
            value = item.get(key)
            if value is None or value == "":
                raise HelperError(
                    f"request files[{index}] is missing required key {key!r}",
                    detail={"index": index, "path": path, "key": key},
                )
        out.append(item)
    return out


def _validate_guest_log_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or _dotdot(path):
        raise HelperError("log path must be absolute and traversal-free", detail={"path": path})
    if QUARANTINE_DIRNAME in path.split("/"):
        raise HelperError(
            "quarantine trees are never log inputs", detail={"path": path}
        )
    root = _log_root_for(path)
    if root is None:
        raise HelperError(
            "path is outside the allowlisted guest log roots",
            detail={"path": path, "allowed": list(GUEST_LOG_ROOTS)},
        )
    return root


def _log_root_for(path: str) -> Optional[str]:
    best: Optional[str] = None
    for root in GUEST_LOG_ROOTS:
        if path == root or path.startswith(root + "/"):
            if best is None or len(root) > len(best):
                best = root
    return best


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #


def _guest_facts(provided: Any, sysroot: str) -> Dict[str, Any]:
    if provided is not None:
        if not isinstance(provided, dict):
            raise HelperError("request 'guest' must be an object")
        name = provided.get("name", provided.get("hostname"))
        os_type = provided.get("os_type", provided.get("ostype"))
        running = provided.get("is_running", provided.get("status"))
        template = provided.get("is_template", provided.get("template"))
        if isinstance(running, str):
            running = running.strip().lower() == "running"
        if template in (1, "1", "true", "True", "yes"):
            template = True
        elif template in (0, "0", "false", "False", "no", None):
            template = False
        else:
            template = bool(template)
        return {
            "name": str(name) if name else "",
            "os_type": str(os_type) if os_type else "",
            "is_running": bool(running),
            "is_template": bool(template),
        }
    hostname = _read_bytes_optional(_join(sysroot, "/etc/hostname"), 512)
    name = hostname.decode("utf-8", "replace").strip() if hostname else os.uname()[1]
    osid = ""

    os_release = _read_bytes_optional(_join(sysroot, "/etc/os-release"), 64 * 1024)
    if os_release:
        for line in os_release.decode("utf-8", "replace").splitlines():
            if line.startswith("ID="):
                osid = line[3:].strip().strip('"')
                break
    return {
        "name": name,
        "os_type": osid,
        "is_running": True,
        "is_template": False,
    }


def _find_binary(sysroot: str, name: str) -> Optional[str]:
    if "/" in name:
        joined = _join(sysroot, name)
        return joined if _is_executable(joined) else None
    for directory in BIN_DIRS:
        joined = _join(sysroot, directory + "/" + name)
        if _is_executable(joined):
            return joined
    return None


def _is_executable(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and bool(st.st_mode & 0o111)


def _default_runner(sysroot: str) -> Runner:
    def run(argv: Sequence[str]) -> Optional[Tuple[int, str, str]]:
        exe = _find_binary(sysroot, argv[0])
        if exe is None:
            return None
        try:
            proc = subprocess.run(
                [exe] + list(argv[1:]),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=EXEC_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    return run


def _run_first_line(runner: Runner, argv: Sequence[str]) -> Optional[str]:
    try:
        result = runner(argv)
    except Exception:  # noqa: BLE001 - probing must not abort on tool errors
        return None
    if not result:
        return None
    rc, out, _err = result
    if rc != 0:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _run_text(runner: Runner, argv: Sequence[str], limit: int) -> Optional[str]:
    """Run a bounded read-only query; over-limit/nonzero/missing output is None."""
    try:
        result = runner(argv)
    except Exception:  # noqa: BLE001 - probing must not abort on tool errors
        return None
    if not result:
        return None
    rc, out, _err = result
    if rc != 0:
        return None
    if len(out.encode("utf-8", "replace")) > limit:
        return None
    return out


def _bun_prune_supported(runner: Runner) -> bool:
    """True only for rc0 help advertising ``bun pm cache prune --max-age``.

    ``bun pm cache prune --dry-run`` is never used as feature detection: older
    builds can accept an unknown subcommand and exit zero without pruning.
    """
    text = _run_text(runner, ("bun", "pm", "--help"), MAX_BUN_HELP_BYTES)
    if text is None:
        return False
    clean = _ANSI_RE.sub("", text)
    if len(clean.encode("utf-8", "replace")) > MAX_BUN_HELP_BYTES:
        return False
    normalized = " ".join(clean.split())
    return bool(
        re.search(r"(?<![\w.-])bun\s+pm\s+cache\s+prune(?![\w.-])", normalized)
        and re.search(r"(?<![\w.-])--max-age(?![\w.-])", normalized)
    )


def _lookup_user(sysroot: str, name: str) -> Optional[Tuple[int, int]]:
    data = _read_bytes_optional(_join(sysroot, "/etc/passwd"), 512 * 1024)
    if data is None:
        return None
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.split(":")
        if len(parts) >= 4 and parts[0] == name:
            try:
                return int(parts[2]), int(parts[3])
            except ValueError:
                return None
    return None


def _mode_access(st: os.stat_result, uid: int, gid: int, need: int) -> bool:
    if st.st_uid == uid and (st.st_mode & (need << 6)) == need << 6:
        return True
    if st.st_gid == gid and (st.st_mode & (need << 3)) == need << 3:
        return True
    return (st.st_mode & need) == need


def _group_names(sysroot: str, gid: int) -> Set[str]:
    names: Set[str] = set()
    data = _read_bytes_optional(_join(sysroot, "/etc/group"), 512 * 1024)
    if data is None:
        return names
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[2].isdigit() and int(parts[2]) == gid:
            names.add(parts[0])
    return names


def _default_acl_grants(
    sysroot: str,
    directory: str,
    alloy_name: str,
    alloy_gid: int,
    group_names: Set[str],
    runner: Runner,
) -> bool:
    getfacl = _find_binary(sysroot, "getfacl")
    if getfacl is None:
        return False
    try:
        result = runner(["getfacl", "-p", directory])
    except Exception:  # noqa: BLE001
        return False
    if not result:
        return False
    rc, out, _err = result
    if rc != 0:
        return False
    permissions: Dict[str, str] = {}
    for line in out.splitlines():
        parts = line.strip().split("#", 1)[0].strip().split(":")
        if len(parts) == 4 and parts[0] == "default":
            permissions[":".join(parts[1:3])] = parts[3]
    named = permissions.get("user:" + alloy_name, "")
    mask = permissions.get("mask:", "")
    return all(flag in named and flag in mask for flag in ("r", "x"))


def _alloy_path_access(paths: Sequence[Tuple[str, int]], runner: Runner) -> Dict[str, bool]:
    """Check effective ACL/mask/ancestor permissions as the real Alloy user."""
    answer: Dict[str, bool] = {}
    code = (
        "import json,os,sys;"
        "print(json.dumps({p:os.access(p,m) for p,m in json.loads(sys.argv[1])}))"
    )
    batch: List[Tuple[str, int]] = []
    size = 0
    for item in list(paths) + [("", 0)]:
        item_size = len(json.dumps(item).encode("utf-8"))
        if batch and (size + item_size > 48 * 1024 or not item[0]):
            try:
                result = runner(["runuser", "-u", "alloy", "--", "python3", "-c", code, json.dumps(batch)])
                if result and result[0] == 0:
                    data = json.loads(result[1])
                    answer.update({p: data.get(p) is True for p, _ in batch})
            except (OSError, ValueError, TypeError):
                pass
            batch = []
            size = 0
        if item[0]:
            batch.append(item)
            size += item_size
    return answer


def _log_access(
    sysroot: str,
    files: Sequence[Dict[str, Any]],
    dirs: Sequence[str],
    runner: Runner,
) -> Tuple[bool, Dict[str, Any]]:
    detail: Dict[str, Any] = {
        "alloy_user": False,
        "dirs": {},
        "files": {},
    }
    alloy = _lookup_user(sysroot, "alloy")
    if alloy is None:
        return False, detail
    uid, gid = alloy
    detail["alloy_user"] = True
    names = _group_names(sysroot, gid)
    ready = bool(files or dirs)
    effective = (
        _alloy_path_access(
            [(directory, os.R_OK | os.X_OK) for directory in sorted(set(dirs))]
            + [(entry["path"], os.R_OK) for entry in files], runner,
        )
        if sysroot == "/" else None
    )
    for directory in sorted(set(dirs)):
        joined = _join(sysroot, directory)
        try:
            st = os.stat(joined)
        except OSError as exc:
            raise HelperError(
                f"failed to stat log directory {directory}: {exc.strerror or exc}",
                detail={"path": directory},
            )
        traversable = (
            effective.get(directory, False) if effective is not None else _mode_access(st, uid, gid, 0o5)
        )
        acl = _default_acl_grants(sysroot, joined, "alloy", gid, names, runner)
        detail["dirs"][directory] = {"traversable": traversable, "default_acl": acl}
        if not (traversable and acl):
            ready = False
    for entry in files:
        path = entry["path"]
        joined = _join(sysroot, path)
        try:
            st = os.lstat(joined)
        except OSError as exc:
            raise HelperError(
                f"failed to stat log file {path}: {exc.strerror or exc}",
                detail={"path": path},
            )
        readable = stat.S_ISREG(st.st_mode) and (
            effective.get(path, False) if effective is not None else _mode_access(st, uid, gid, 0o4)
        )
        detail["files"][path] = readable
        if not readable:
            ready = False
    return ready, detail


def _profile_evidence(sysroot: str) -> Dict[str, Dict[str, Any]]:
    def unit(name: str) -> bool:
        return any(os.path.exists(_join(sysroot, d + "/" + name)) for d in UNIT_DIRS)

    npm: Dict[str, Any] = {
        "npm.service": unit("npm.service"),
        "openresty.service": unit("openresty.service"),
        "app_root": os.path.isdir(_join(sysroot, NPM_APP_ROOT)),
        "log_root": os.path.isdir(_join(sysroot, NPM_LOG_ROOT)),
    }
    pbs: Dict[str, Any] = {
        "proxmox-backup-manager": _find_binary(sysroot, "proxmox-backup-manager") is not None,
        "proxmox-backup-proxy.service": unit("proxmox-backup-proxy.service"),
        "task_root": os.path.isdir(_join(sysroot, PBS_TASK_ROOT)),
        "api_root": os.path.isdir(_join(sysroot, PBS_API_ROOT)),
    }
    npm["detected"] = all(npm.values())
    pbs["detected"] = all(pbs.values())
    return {"npm": npm, "pbs": pbs}


# --------------------------------------------------------------------------- #
# Catalogue logging evidence (read-only)
# --------------------------------------------------------------------------- #
#
# The manager resolves the catalogue and sends a fully resolved descriptor of
# the selected owners/includes/layouts/sources.  This helper only *measures*
# the requested keys: unit activity through fixed ``systemctl`` argv, directory
# metadata without following symlinks, and effective Alloy-user access.  It
# never interprets a manifest, evaluates a predicate, or echoes app/component
# labels back as proof; the pure manager-side validator owns policy.

_MAX_LOGGING_SERVICES = 512
_MAX_LOGGING_UNITS = 256
_MAX_LOGGING_LAYOUTS = 16
_MAX_LOGGING_PATHS = 64
_MAX_LOGGING_ROOTS = 64
_MAX_LOGGING_EXCLUDES = 64
_MAX_LOGGING_ENTRIES = 100_000
#: Per-source identity ceiling.  This is deliberately far above the observed
#: PBS population (898 UPIDs) and is not the primary bound: entry/time limits
#: and the serialized response budget below are.  Reaching it marks the source
#: partial -- identities are never silently sampled.
_MAX_LOGGING_FILES = 50_000
_MAX_LOGGING_INSTANCES = 64
_MAX_LOGGING_SECONDS = 60.0
#: Bound on the compact serialized logging evidence returned by the helper.
#: Probes are staged over the runner's event transport, so an unbounded
#: source/identity set must fail closed (structured partial), never truncate
#: ownership proof silently.
_MAX_LOGGING_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_LOGGING_DEPTH = 64
_MAX_LOGGING_ERROR_CHARS = 200
_MAX_LOGGING_PATH_BYTES = 4096
_SERVICE_ID_RE = re.compile(r"\A[a-z][a-z0-9-]{0,63}\Z")
_SOURCE_KEY_RE = re.compile(
    r"\A[a-z][a-z0-9-]{0,63}(?:/[a-z][a-z0-9-]{0,63}){1,2}\Z"
)
_USER_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.-]{0,31}\Z")
_UNIT_NAME_RE = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9_.@:\\-]*(?:\*[A-Za-z0-9_.@:\\-]*)?"
    r"\.(?:service|socket|timer|target|path|mount|automount|slice|scope|swap|device)\Z"
)


def _logging_error(text: object) -> str:
    cleaned = " ".join(str(text).split())
    cleaned = "".join(
        ch if ord(ch) >= 32 and ord(ch) != 127 else " " for ch in cleaned
    )
    return cleaned[: _MAX_LOGGING_ERROR_CHARS]


def _logging_path(value: object, *, label: str, allow_glob: bool) -> str:
    if not isinstance(value, str) or not value:
        raise HelperError(f"{label} must be a nonempty absolute path")
    if len(value.encode("utf-8")) > _MAX_LOGGING_PATH_BYTES:
        raise HelperError(f"{label} exceeds the bounded path length")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise HelperError(f"{label} must not contain control characters")
    if "{" in value or "}" in value or "\\" in value or "://" in value:
        raise HelperError(f"{label} is not a supported absolute path")
    if not value.startswith("/") or value.startswith("//"):
        raise HelperError(f"{label} must be an absolute normalized path")
    parts = value.split("/")
    if any(part in (".", "..") for part in parts):
        raise HelperError(f"{label} must not contain traversal components")
    if any(part == "" for part in parts[1:]):
        raise HelperError(f"{label} must not contain repeated separators")
    if len(value) > 1 and value.endswith("/"):
        raise HelperError(f"{label} must not end with a slash")
    if value == "/":
        raise HelperError(f"{label} must not be the filesystem root")
    if not allow_glob and any(ch in value for ch in "*?["):
        raise HelperError(f"{label} must not contain glob characters")
    return value


def _logging_path_list(
    value: object,
    *,
    label: str,
    allow_glob: bool,
    limit: int,
    allow_duplicates: bool = False,
) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HelperError(f"{label} must be a list")
    if len(value) > limit:
        raise HelperError(f"{label} exceeds the bounded entry limit")
    out: List[str] = []
    for index, item in enumerate(value):
        path = _logging_path(item, label=f"{label}[{index}]", allow_glob=allow_glob)
        if path in out:
            if allow_duplicates:
                continue
            raise HelperError(f"{label}[{index}] repeats {path!r}")
        out.append(path)
    return out


def _logging_unit(raw: object, *, label: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise HelperError(f"{label} must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not _UNIT_NAME_RE.match(name):
        raise HelperError(f"{label}.name is not a bounded systemd unit name")
    if name.count("*") > 1:
        raise HelperError(f"{label}.name must contain at most one instance glob")
    scope = raw.get("scope", "system")
    if scope not in ("system", "user"):
        raise HelperError(f"{label}.scope must be system or user")
    user = raw.get("user")
    if scope == "user":
        if not isinstance(user, str) or not _USER_NAME_RE.match(user):
            raise HelperError(f"{label} requires a bounded explicit user")
        key = f"user:{user}:{name}"
    else:
        if user is not None:
            raise HelperError(f"{label} is system-scoped but declares a user")
        key = f"system:{name}"
    if raw.get("key") != key:
        raise HelperError(f"{label}.key does not match its scope/name")
    return {"key": key, "name": name, "scope": scope, "user": user}


def _logging_units(
    values: object, *, label: str, into: Dict[str, Dict[str, Any]]
) -> None:
    if values is None:
        return
    if not isinstance(values, list):
        raise HelperError(f"{label} must be a list")
    if len(values) > _MAX_LOGGING_UNITS:
        raise HelperError(f"{label} exceeds the bounded entry limit")
    for index, item in enumerate(values):
        unit = _logging_unit(item, label=f"{label}[{index}]")
        into.setdefault(unit["key"], unit)
    if len(into) > _MAX_LOGGING_UNITS:
        raise HelperError("a service declares too many distinct units")


def _logging_service(
    raw: object, *, label: str, allow_includes: bool
) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise HelperError(f"{label} must be an object")
    layouts = raw.get("layouts")
    if not isinstance(layouts, list) or not layouts:
        raise HelperError(f"{label}.layouts must be a nonempty list")
    if len(layouts) > _MAX_LOGGING_LAYOUTS:
        raise HelperError(f"{label}.layouts exceeds the bounded entry limit")
    units: Dict[str, Dict[str, Any]] = {}
    seen_layouts: Set[str] = set()
    for index, layout in enumerate(layouts):
        if not isinstance(layout, dict):
            raise HelperError(f"{label}.layouts[{index}] must be an object")
        layout_id = layout.get("id")
        if not isinstance(layout_id, str) or not _SERVICE_ID_RE.match(layout_id):
            raise HelperError(f"{label}.layouts[{index}].id is invalid")
        if layout_id in seen_layouts:
            raise HelperError(f"{label} repeats layout {layout_id!r}")
        seen_layouts.add(layout_id)
        _logging_units(
            layout.get("required_units"),
            label=f"{label}.layouts[{index}].required_units",
            into=units,
        )
        _logging_units(
            layout.get("running_units_any"),
            label=f"{label}.layouts[{index}].running_units_any",
            into=units,
        )
    _logging_units(raw.get("journal_units"), label=f"{label}.journal_units", into=units)
    includes: Dict[str, Dict[str, Any]] = {}
    nested = raw.get("includes")
    if nested is not None and not isinstance(nested, dict):
        raise HelperError(f"{label}.includes must be an object")
    if nested:
        if not allow_includes:
            raise HelperError(f"{label} must not declare nested includes")
        for dependency_id, dependency in nested.items():
            if not isinstance(dependency_id, str) or not _SERVICE_ID_RE.match(
                dependency_id
            ):
                raise HelperError(f"{label}.includes has an invalid service id")
            includes[dependency_id] = _logging_service(
                dependency,
                label=f"{label}.includes[{dependency_id!r}]",
                allow_includes=False,
            )
    if len(includes) > _MAX_LOGGING_SERVICES:
        raise HelperError(f"{label}.includes exceeds the bounded entry limit")
    return {"units": units, "includes": includes}


def _logging_source(raw: object, *, label: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise HelperError(f"{label} must be an object")
    layouts = raw.get("layouts")
    if not isinstance(layouts, dict) or not layouts:
        raise HelperError(f"{label}.layouts must be a nonempty object")
    if len(layouts) > _MAX_LOGGING_LAYOUTS:
        raise HelperError(f"{label}.layouts exceeds the bounded entry limit")
    parsed: Dict[str, Any] = {}
    for layout_id, descriptor in layouts.items():
        if not isinstance(layout_id, str) or not _SERVICE_ID_RE.match(layout_id):
            raise HelperError(f"{label} has an invalid layout id")
        if not isinstance(descriptor, dict):
            raise HelperError(f"{label}.layouts[{layout_id!r}] must be an object")
        layout_label = f"{label}.layouts[{layout_id!r}]"
        roots = _logging_path_list(
            descriptor.get("roots"),
            label=layout_label + ".roots",
            allow_glob=False,
            limit=_MAX_LOGGING_ROOTS,
        )
        paths = _logging_path_list(
            descriptor.get("paths"),
            label=layout_label + ".paths",
            allow_glob=True,
            limit=_MAX_LOGGING_PATHS,
        )
        exact_files = _logging_path_list(
            descriptor.get("exact_files"),
            label=layout_label + ".exact_files",
            allow_glob=False,
            limit=_MAX_LOGGING_PATHS,
        )
        excludes = _logging_path_list(
            descriptor.get("exclude_paths"),
            label=layout_label + ".exclude_paths",
            allow_glob=True,
            limit=_MAX_LOGGING_EXCLUDES,
        )
        required = descriptor.get("required", True)
        if not isinstance(required, bool):
            raise HelperError(f"{layout_label}.required must be a boolean")
        for pattern in paths:
            prefix = _literal_prefix(pattern)
            if not any(prefix.startswith(root + "/") for root in roots):
                raise HelperError(
                    f"{layout_label} declares a path outside its effective roots"
                )
        if not paths and not exact_files:
            raise HelperError(f"{label} has no configured reader")
        parsed[layout_id] = {
            "roots": roots,
            "paths": paths,
            "exact_files": exact_files,
            "exclude_paths": excludes,
            "required": required,
        }
    return {"layouts": parsed}


def _parse_logging_request(raw: object) -> Dict[str, Any]:
    """Validate the manager-built logging descriptor before any measurement."""
    if not isinstance(raw, dict):
        raise HelperError("request 'logging' must be an object")
    plugins_raw = raw.get("plugins", {})
    sources_raw = raw.get("sources", {})
    if not isinstance(plugins_raw, dict) or not isinstance(sources_raw, dict):
        raise HelperError("logging 'plugins'/'sources' must be objects")
    if len(plugins_raw) > _MAX_LOGGING_SERVICES:
        raise HelperError("logging plugins exceed the bounded entry limit")
    plugins: Dict[str, Dict[str, Any]] = {}
    for owner_id, entry in plugins_raw.items():
        if not isinstance(owner_id, str) or not _SERVICE_ID_RE.match(owner_id):
            raise HelperError("logging has an invalid owner id")
        plugins[owner_id] = _logging_service(
            entry, label=f"logging.plugins[{owner_id!r}]", allow_includes=True
        )
    if len(sources_raw) > _MAX_LOGGING_SERVICES:
        raise HelperError("logging sources exceed the bounded entry limit")
    sources: Dict[str, Dict[str, Any]] = {}
    for key, entry in sources_raw.items():
        if not isinstance(key, str) or not _SOURCE_KEY_RE.match(key):
            raise HelperError("logging has an invalid source key")
        sources[key] = _logging_source(entry, label=f"logging.sources[{key!r}]")
    return {"plugins": plugins, "sources": sources}


def _empty_logging_facts() -> Dict[str, Any]:
    return {"plugins": {}, "sources": {}}


# -- unit activity ---------------------------------------------------------- #


def _systemctl_prefix(
    scope: str, user: Optional[str], sysroot: str
) -> Optional[List[str]]:
    if scope == "system":
        return ["systemctl"]
    if not user:
        return None
    account = _lookup_user(sysroot, user)
    if account is None:
        return None
    uid = account[0]
    run_dir = _join(sysroot, "/run/user/{0}".format(uid))
    try:
        st = os.stat(run_dir)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    return [
        "runuser",
        "-u",
        user,
        "--",
        "env",
        "XDG_RUNTIME_DIR=/run/user/{0}".format(uid),
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{0}/bus".format(uid),
        "systemctl",
        "--user",
    ]


def _show_unit_state(
    prefix: Sequence[str], unit: str, runner: Runner
) -> Optional[str]:
    argv = [
        *prefix,
        "show",
        "--no-pager",
        "--property=LoadState",
        "--property=ActiveState",
        "--",
        unit,
    ]
    try:
        result = runner(argv)
    except Exception:  # noqa: BLE001 - probing must not abort on tool errors
        return None
    if not result:
        return None
    _rc, out, _err = result
    values: Dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    if values.get("LoadState") == "not-found":
        return "missing"
    if "LoadState" not in values or "ActiveState" not in values:
        return None
    return "active" if values["ActiveState"] == "active" else "inactive"


def _glob_unit_evidence(
    prefix: Sequence[str],
    name: str,
    runner: Runner,
) -> Dict[str, Any]:
    argv = [*prefix, "list-unit-files", "--no-legend", "--no-pager", "--", name]
    try:
        result = runner(argv)
    except Exception:  # noqa: BLE001
        return {"status": "unavailable", "instances": {}}
    if not result:
        return {"status": "unavailable", "instances": {}}
    rc, out, _err = result
    if rc != 0:
        return {"status": "unavailable", "instances": {}}
    instances: Dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        candidate = parts[0]
        if "*" in candidate or "/" in candidate or "@." in candidate:
            # ``@.service`` is the uninstantiated template, not an instance.
            continue
        if not fnmatch.fnmatchcase(candidate, name):
            continue
        status = _show_unit_state(prefix, candidate, runner)
        instances[candidate] = status or "unavailable"
    if len(instances) > _MAX_LOGGING_INSTANCES:
        return {"status": "unavailable", "instances": {}}
    if not instances:
        return {"status": "missing", "instances": {}}
    if "active" in instances.values():
        derived = "active"
    elif "unavailable" in instances.values():
        derived = "unavailable"
    elif "inactive" in instances.values():
        derived = "inactive"
    else:
        derived = "missing"
    return {"status": derived, "instances": instances}


def _unit_evidence(
    unit: Dict[str, Any],
    cache: Dict[str, Dict[str, Any]],
    *,
    sysroot: str,
    runner: Runner,
) -> Dict[str, Any]:
    key = unit["key"]
    cached = cache.get(key)
    if cached is not None:
        return cached
    prefix = _systemctl_prefix(unit["scope"], unit["user"], sysroot)
    if prefix is None:
        record = {"status": "unavailable", "instances": {}}
    elif "*" in unit["name"]:
        record = _glob_unit_evidence(prefix, unit["name"], runner)
    else:
        status = _show_unit_state(prefix, unit["name"], runner)
        record = {"status": status or "unavailable", "instances": {}}
    cache[key] = record
    return record


# -- access measurement ----------------------------------------------------- #


def _alloy_access_probe(
    paths: Sequence[Tuple[str, int]], sysroot: str, runner: Runner
) -> Dict[str, Optional[bool]]:
    """Effective Alloy-user access, preserving "unmeasurable" as ``None``."""
    answer: Dict[str, Optional[bool]] = {path: None for path, _ in paths}
    if sysroot != "/":
        return answer
    if _find_binary(sysroot, "runuser") is None or _find_binary(sysroot, "python3") is None:
        return answer
    code = (
        "import json,os,sys;"
        "print(json.dumps({p:os.access(p,m) for p,m in json.loads(sys.argv[1])}))"
    )
    batch: List[Tuple[str, int]] = []
    size = 0

    def flush() -> None:
        nonlocal batch, size
        if not batch:
            return
        try:
            result = runner(
                ["runuser", "-u", "alloy", "--", "python3", "-c", code, json.dumps(batch)]
            )
        except Exception:  # noqa: BLE001
            result = None
        if result and result[0] == 0:
            try:
                data = json.loads(result[1])
            except (TypeError, ValueError):
                data = None
            if isinstance(data, dict):
                for path, _mode in batch:
                    value = data.get(path)
                    if isinstance(value, bool):
                        answer[path] = value
        batch = []
        size = 0

    for item in list(paths):
        item_size = len(json.dumps(item).encode("utf-8"))
        if batch and size + item_size > 48 * 1024:
            flush()
        batch.append(item)
        size += item_size
    flush()
    return answer


def _default_acl_tristate(
    sysroot: str,
    directory: str,
    runner: Runner,
) -> Optional[bool]:
    """Whether the directory grants the Alloy user ``r-x`` through its default ACL."""
    if _find_binary(sysroot, "getfacl") is None:
        return None
    try:
        result = runner(["getfacl", "-p", directory])
    except Exception:  # noqa: BLE001
        return None
    if not result:
        return None
    rc, out, _err = result
    if rc != 0:
        return None
    permissions: Dict[str, str] = {}
    for line in out.splitlines():
        parts = line.strip().split("#", 1)[0].strip().split(":")
        if len(parts) == 4 and parts[0] == "default":
            permissions[":".join(parts[1:3])] = parts[3]
    named = permissions.get("user:alloy", "")
    mask = permissions.get("mask:", "")
    return all(flag in named and flag in mask for flag in ("r", "x"))


def _mode_traverse(
    sysroot: str, guest_path: str, uid: int, gid: int
) -> Optional[bool]:
    """Mode-bit fallback traversal check for test sysroots (all components)."""
    try:
        root_st = os.stat(sysroot)
    except OSError:
        return None
    if not _mode_access(root_st, uid, gid, 0o1):
        return False
    current = ""
    for part in [item for item in guest_path.split("/") if item]:
        current += "/" + part
        try:
            st = os.stat(_join(sysroot, current))
        except FileNotFoundError:
            return False
        except OSError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            return False
        if not _mode_access(st, uid, gid, 0o1):
            return False
    return True


def _traverse_access(
    sysroot: str,
    guest_path: str,
    alloy: Optional[Tuple[int, int]],
    runner: Runner,
) -> Optional[bool]:
    if alloy is None:
        return None
    if sysroot == "/":
        measured = _alloy_access_probe([(guest_path, os.X_OK)], sysroot, runner)
        return measured.get(guest_path)
    return _mode_traverse(sysroot, guest_path, alloy[0], alloy[1])


def _root_access(
    st: os.stat_result,
    root: str,
    *,
    sysroot: str,
    alloy: Optional[Tuple[int, int]],
    runner: Runner,
) -> Tuple[Optional[Dict[str, bool]], str]:
    if alloy is None:
        return None, "unavailable"
    uid, gid = alloy
    acl = _default_acl_tristate(sysroot, _join(sysroot, root), runner)
    if acl is None:
        return None, "unavailable"
    traversable: Optional[bool]
    readable: Optional[bool]
    if sysroot == "/":
        measured = _alloy_access_probe([(root, os.R_OK | os.X_OK)], sysroot, runner)
        value = measured.get(root)
        if value is None:
            return None, "unavailable"
        traversable = readable = value
    else:
        traversable = _mode_traverse(sysroot, root, uid, gid)
        readable = _mode_access(st, uid, gid, 0o4)
        if traversable is None:
            return None, "unavailable"
    access = {
        "traversable": bool(traversable),
        "readable": bool(readable),
        "default_acl": bool(acl),
    }
    status = "ok" if (traversable and readable and acl) else "denied"
    return access, status


def _unsafe_ancestors(sysroot: str, guest_path: str) -> bool:
    current = ""
    parts = [item for item in guest_path.split("/") if item]
    for part in parts[:-1]:
        current += "/" + part
        try:
            st = os.lstat(_join(sysroot, current))
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            return True
    return False


# -- source metadata -------------------------------------------------------- #


def _literal_prefix(pattern: str) -> str:
    indexes = [index for ch in "*?[" if (index := pattern.find(ch)) >= 0]
    return pattern[: min(indexes)] if indexes else pattern


_MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def _unescape_mount(text: str) -> str:
    def replace(match: "re.Match[str]") -> str:
        return chr(int(match.group(1), 8))

    return _MOUNT_ESCAPE_RE.sub(replace, text)


def _read_mountpoints(sysroot: str) -> Optional[Set[str]]:
    """Guest-absolute mountpoints, never guessed when the table is unavailable.

    A fixture sysroot may carry its own ``/proc/self/mountinfo``; otherwise the
    host table is translated for the sysroot prefix so tests and the node-side
    pinned root share one code path.  ``None`` means mount safety is
    unmeasurable and the caller must not claim the source is safe.
    """
    data: Optional[bytes] = None
    translate = False
    fixture = _join(sysroot, "/proc/self/mountinfo")
    try:
        if os.path.isfile(fixture):
            data = _read_bytes_optional(fixture, 4 * 1024 * 1024)
    except HelperError:
        return None
    if data is None:
        try:
            data = _read_bytes_optional("/proc/self/mountinfo", 4 * 1024 * 1024)
        except HelperError:
            return None
        translate = sysroot != "/"
    if data is None:
        return None
    root = sysroot.rstrip("/")
    mountpoints: Set[str] = set()
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        mount = _unescape_mount(parts[4])
        if not mount.startswith("/"):
            continue
        if translate:
            if root and mount == root:
                guest_path = "/"
            elif root and mount.startswith(root + "/"):
                guest_path = mount[len(root):]
            else:
                continue
        else:
            guest_path = mount
        if guest_path != "/" and _dotdot(guest_path):
            continue
        mountpoints.add(guest_path)
    return mountpoints


def _pattern_could_match_beneath(pattern: str, directory: str) -> bool:
    """Whether any regular file strictly below ``directory`` could match."""
    dir_parts = [item for item in directory.split("/") if item]
    pat_parts = [item for item in pattern.split("/") if item]
    memo: Dict[Tuple[int, int], bool] = {}

    def possible(pattern_index: int, dir_index: int) -> bool:
        key = (pattern_index, dir_index)
        cached = memo.get(key)
        if cached is not None:
            return cached
        if dir_index == len(dir_parts):
            result = bool(pat_parts[pattern_index:])
        elif pattern_index == len(pat_parts):
            result = False
        elif pat_parts[pattern_index] == "**":
            result = possible(pattern_index + 1, dir_index) or possible(
                pattern_index, dir_index + 1
            )
        else:
            result = fnmatch.fnmatchcase(
                dir_parts[dir_index], pat_parts[pattern_index]
            ) and possible(pattern_index + 1, dir_index + 1)
        memo[key] = result
        return result

    return possible(0, 0)


def _glob_match(path: str, pattern: str) -> bool:
    """Segment-aware path glob (``**`` spans directories, ``*`` does not)."""
    path_parts = [item for item in path.split("/") if item]
    pattern_parts = [item for item in pattern.split("/") if item]
    memo: Dict[Tuple[int, int], bool] = {}

    def match(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        cached = memo.get(key)
        if cached is not None:
            return cached
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = any(
                match(index, pattern_index + 1)
                for index in range(path_index, len(path_parts) + 1)
            )
        elif path_index == len(path_parts):
            result = False
        elif fnmatch.fnmatchcase(
            path_parts[path_index], pattern_parts[pattern_index]
        ):
            result = match(path_index + 1, pattern_index + 1)
        else:
            result = False
        memo[key] = result
        return result

    return match(0, 0)


def _probe_log_root(
    root: str,
    *,
    sysroot: str,
    alloy: Optional[Tuple[int, int]],
    runner: Runner,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "exists": False,
        "is_dir": False,
        "is_symlink": False,
        "device": None,
        "blocked_ancestor": False,
        "status": "missing_root",
        "error": None,
    }
    joined = _join(sysroot, root)
    try:
        observed = os.lstat(joined)
    except FileNotFoundError:
        return record
    except OSError as exc:
        record["status"] = "failed"
        record["error"] = _logging_error(exc.strerror or exc)
        return record
    record["exists"] = True
    if stat.S_ISLNK(observed.st_mode):
        record["is_symlink"] = True
        record["device"] = int(observed.st_dev)
        record["status"] = "failed"
        record["error"] = "declared log root is a symbolic link"
        return record
    if not stat.S_ISDIR(observed.st_mode):
        record["device"] = int(observed.st_dev)
        record["status"] = "failed"
        record["error"] = "declared log root is not a directory"
        return record
    record["is_dir"] = True
    record["device"] = int(observed.st_dev)
    if _unsafe_ancestors(sysroot, root):
        record["blocked_ancestor"] = True
        record["status"] = "failed"
        record["error"] = "declared log root has a symbolic-link ancestor"
        return record
    try:
        fd = _open_dir_path(joined)
    except HelperError as exc:
        record["status"] = "failed"
        record["error"] = _logging_error(exc)
        return record
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    record["device"] = int(st.st_dev)
    access, status = _root_access(
        st, root, sysroot=sysroot, alloy=alloy, runner=runner
    )
    if access is not None:
        record["access"] = access
    record["status"] = status
    return record


def _behind_mount(path: str, mounts: Set[str]) -> bool:
    current = ""
    for part in [item for item in path.split("/") if item]:
        current += "/" + part
        if current in mounts:
            return True
    return False


def _absent_exact_status(parent_traversable: Optional[bool]) -> str:
    """Status for an absent declared file whose parent descriptor *was* opened.

    A successful no-follow parent open proves the parent exists; it is never a
    missing root.  A parent the Alloy user cannot traverse is a repairable
    ACL-needed finding (``denied``), not a mapping failure.  ``None`` means the
    collector identity is unknown, so absence stays unmeasurable.
    """
    if parent_traversable is None:
        return "unavailable"
    if parent_traversable:
        return "quiet"
    return "denied"


def _probe_exact_file(
    path: str,
    *,
    sysroot: str,
    alloy: Optional[Tuple[int, int]],
    runner: Runner,
    sysroot_dev: Optional[int],
    mounts: Set[str],
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "exists": False,
        "is_symlink": False,
        "device": None,
        "inode": None,
        "size": None,
        "mtime_ns": None,
        "status": "missing_root",
        "error": None,
    }
    parent = path.rsplit("/", 1)[0] or "/"
    parent_traversable = _traverse_access(sysroot, parent, alloy, runner)
    if parent_traversable is not None:
        record["parent_traversable"] = bool(parent_traversable)
    try:
        parent_fd, basename = _open_parent(_join(sysroot, path))
    except HelperError as exc:
        # ``_open_parent`` refused or could not reach the parent.  Only an
        # explicit ENOENT proves the parent (and therefore the file) absent;
        # a symlinked/non-directory/unreadable parent must fail closed rather
        # than masquerade as a missing optional file.
        missing_parent = exc.detail.get("errno") == errno.ENOENT
        observed: Optional[os.stat_result] = None
        try:
            observed = os.lstat(_join(sysroot, path))
        except OSError:
            observed = None
        if observed is not None:
            record["exists"] = True
            record["is_symlink"] = stat.S_ISLNK(observed.st_mode)
            record["device"] = int(observed.st_dev)
            record["inode"] = int(observed.st_ino)
        if missing_parent and observed is None:
            # No safe parent descriptor exists.  A parent that was traversable
            # a moment ago disappeared (or the collector identity is unknown):
            # unmeasurable, never a fabricated missing mapping.  A parent the
            # Alloy user could not traverse is genuinely missing.
            record["status"] = (
                "missing_root" if parent_traversable is False else "unavailable"
            )
            return record
        record["status"] = "failed"
        record["error"] = _logging_error(exc)
        return record
    try:
        try:
            observed = _stat_at(parent_fd, basename)
        except HelperError as exc:
            record["status"] = "failed"
            record["error"] = _logging_error(exc)
            return record
    finally:
        os.close(parent_fd)
    if observed is None:
        if _behind_mount(path, mounts):
            record["status"] = "failed"
            record["error"] = "declared exact file is behind a mount escape"
            return record
        record["status"] = _absent_exact_status(parent_traversable)
        return record
    record["exists"] = True
    record["is_symlink"] = stat.S_ISLNK(observed.st_mode)
    record["device"] = int(observed.st_dev)
    record["inode"] = int(observed.st_ino)
    record["size"] = int(observed.st_size)
    record["mtime_ns"] = int(observed.st_mtime_ns)
    if sysroot_dev is not None:
        record["same_device"] = int(observed.st_dev) == int(sysroot_dev)
    if record["is_symlink"]:
        record["status"] = "failed"
        record["error"] = "declared exact file is a symbolic link"
        return record
    if not stat.S_ISREG(observed.st_mode):
        record["is_file"] = False
        record["status"] = "failed"
        record["error"] = "declared exact file is not a regular file"
        return record
    record["is_file"] = True
    if record.get("same_device") is False:
        record["status"] = "failed"
        record["error"] = "declared exact file is on a different filesystem"
        return record
    if _behind_mount(path, mounts):
        record["status"] = "failed"
        record["error"] = "declared exact file is behind a mount escape"
        return record
    if alloy is None:
        record["status"] = "unavailable"
        return record
    if sysroot == "/":
        measured = _alloy_access_probe([(path, os.R_OK)], sysroot, runner)
        readable = measured.get(path)
    else:
        readable = _mode_access(observed, alloy[0], alloy[1], 0o4)
        if readable and parent_traversable is not True:
            readable = False
    if readable is None or parent_traversable is None:
        record["status"] = "unavailable"
        return record
    record["access"] = {"readable": bool(readable)}
    if readable and parent_traversable:
        record["status"] = "ok"
    else:
        record["status"] = "denied"
    return record


def _walk_log_root(
    root: str,
    device: int,
    patterns: Sequence[str],
    excludes: Sequence[str],
    states: Dict[str, Dict[str, Any]],
    budget: List[int],
    entry_budget: List[int],
    mounts: Set[str],
    *,
    sysroot: str,
    clock: Callable[[], float],
    deadline: float,
) -> Dict[str, Any]:
    joined = _join(sysroot, root)
    stats = {pattern: states[pattern]["stats"] for pattern in patterns}
    trunk: Dict[str, Any] = {"truncated": False, "error": None}

    try:
        root_fd = _open_dir_path(joined)
    except HelperError as exc:
        return {"truncated": False, "error": _logging_error(exc)}

    def excluded(path: str) -> bool:
        return any(_glob_match(path, pattern) for pattern in excludes)

    def matching(path: str) -> bool:
        return any(_glob_match(path, pattern) for pattern in patterns)

    def fails_beneath(path: str) -> bool:
        return any(
            _pattern_could_match_beneath(pattern, path) for pattern in patterns
        )

    def scan(dir_fd: int, dir_guest: str, depth: int) -> None:
        if depth > _MAX_LOGGING_DEPTH:
            trunk["truncated"] = True
            return
        try:
            iterator = os.scandir(dir_fd)
        except OSError as exc:
            raise HelperError(
                "failed to enumerate {0}: {1}".format(
                    dir_guest, exc.strerror or exc
                )
            ) from exc
        with iterator:
            for entry in iterator:
                entry_budget[0] -= 1
                if entry_budget[0] < 0 or clock() > deadline:
                    trunk["truncated"] = True
                    return
                try:
                    st = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise HelperError(
                        "failed to stat {0}/{1}: {2}".format(
                            dir_guest, entry.name, exc.strerror or exc
                        )
                    ) from exc
                path = dir_guest.rstrip("/") + "/" + entry.name
                mode = st.st_mode
                if stat.S_ISLNK(mode):
                    if excluded(path):
                        continue
                    if matching(path) or fails_beneath(path):
                        raise HelperError(
                            "refusing a symbolic link the reader glob could "
                            "match or traverse: " + path
                        )
                    continue
                if stat.S_ISDIR(mode):
                    if entry.name == QUARANTINE_DIRNAME:
                        continue
                    if excluded(path):
                        continue
                    escape = path in mounts or int(st.st_dev) != device
                    if escape:
                        if fails_beneath(path):
                            raise HelperError(
                                "refusing a mount/symlink escape the reader "
                                "glob would traverse: " + path
                            )
                        continue
                    try:
                        child = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=dir_fd,
                        )
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise HelperError(
                            "cannot traverse {0}/{1}: {2}".format(
                                dir_guest, entry.name, exc.strerror or exc
                            )
                        ) from exc
                    try:
                        scan(child, path, depth + 1)
                    finally:
                        os.close(child)
                    if trunk["truncated"]:
                        return
                    continue
                if excluded(path):
                    continue
                escape = path in mounts or int(st.st_dev) != device
                if escape:
                    if matching(path):
                        raise HelperError(
                            "refusing a mount/symlink escape the reader glob "
                            "matches: " + path
                        )
                    continue
                if not stat.S_ISREG(mode):
                    continue
                for pattern in patterns:
                    if not _glob_match(path, pattern):
                        continue
                    states[pattern]["matched"] += 1
                    if budget[0] > 0:
                        stats[pattern].append((path, st))
                        budget[0] -= 1

    try:
        try:
            scan(root_fd, root, 0)
        except HelperError as exc:
            trunk["error"] = _logging_error(exc)
    finally:
        os.close(root_fd)
    return {"truncated": trunk["truncated"], "error": trunk["error"]}


def _probe_source_layout(
    spec: Dict[str, Any],
    *,
    sysroot: str,
    alloy: Optional[Tuple[int, int]],
    runner: Runner,
    clock: Callable[[], float],
    deadline: float,
    sysroot_dev: Optional[int],
    mounts: Optional[Set[str]],
) -> Dict[str, Any]:
    roots: List[str] = spec["roots"]
    paths: List[str] = spec["paths"]
    exact_files: List[str] = spec["exact_files"]
    required = bool(spec["required"])

    if mounts is None:
        return _unavailable_source_facts(
            roots, paths, exact_files, "the guest mount table is unavailable"
        )
    if clock() > deadline:
        return _partial_source_facts(roots, paths, exact_files)

    root_records: Dict[str, Dict[str, Any]] = {}
    for root in roots:
        root_records[root] = _probe_log_root(
            root, sysroot=sysroot, alloy=alloy, runner=runner
        )

    pattern_root: Dict[str, str] = {}
    for pattern in paths:
        prefix = _literal_prefix(pattern)
        candidates = [
            root for root in roots if prefix.startswith(root + "/")
        ]
        if not candidates:
            raise HelperError(
                "a source path is not beneath any of its effective roots"
            )
        pattern_root[pattern] = max(candidates, key=len)

    states: Dict[str, Dict[str, Any]] = {
        pattern: {"matched": 0, "stats": [], "status": None, "error": None}
        for pattern in paths
    }
    grouped: Dict[str, List[str]] = {}
    for pattern in paths:
        grouped.setdefault(pattern_root[pattern], []).append(pattern)

    truncated = False
    budget = [_MAX_LOGGING_FILES]
    entry_budget = [_MAX_LOGGING_ENTRIES]
    for root in roots:
        grouped_patterns = grouped.get(root, [])
        if not grouped_patterns:
            continue
        record = root_records[root]
        if record["status"] != "ok":
            for pattern in grouped_patterns:
                states[pattern]["status"] = record["status"]
                states[pattern]["error"] = record.get("error")
            continue
        if clock() > deadline:
            truncated = True
            for pattern in grouped_patterns:
                states[pattern]["status"] = "partial"
            continue
        walked = _walk_log_root(
            root,
            int(record["device"]),
            grouped_patterns,
            spec["exclude_paths"],
            states,
            budget,
            entry_budget,
            mounts,
            sysroot=sysroot,
            clock=clock,
            deadline=deadline,
        )
        if walked["error"] is not None:
            record["status"] = "failed"
            record["error"] = walked["error"]
            for pattern in grouped_patterns:
                states[pattern]["status"] = "failed"
                states[pattern]["error"] = walked["error"]
            continue
        if walked["truncated"]:
            truncated = True
            for pattern in grouped_patterns:
                states[pattern]["status"] = "partial"

    if any(
        int(states[pattern]["matched"]) > len(states[pattern]["stats"])
        for pattern in paths
    ):
        # The per-source identity ceiling was reached.  Ownership proof is
        # incomplete, so every still-unclassified pattern becomes partial; no
        # dropped identity is attributed to ``skipped``.
        truncated = True
        for pattern in paths:
            if states[pattern]["status"] is None:
                states[pattern]["status"] = "partial"

    exact_records: Dict[str, Dict[str, Any]] = {}
    for path in exact_files:
        exact_records[path] = _probe_exact_file(
            path,
            sysroot=sysroot,
            alloy=alloy,
            runner=runner,
            sysroot_dev=sysroot_dev,
            mounts=mounts,
        )

    # Batch the effective Alloy-user read check for every collected file of an
    # accessible root.  ``None`` means unmeasurable, never "denied".
    measured_paths: Dict[str, Optional[bool]] = {}
    pending: List[Tuple[str, int]] = []
    for pattern in paths:
        state = states[pattern]
        if state["status"] in (None, "ok", "quiet"):
            for path, _st in state["stats"]:
                pending.append((path, os.R_OK))
    if pending and alloy is not None:
        if sysroot == "/":
            measured_paths = _alloy_access_probe(pending, sysroot, runner)
        else:
            for path, _st in pending:
                measured_paths[path] = None
    matches: List[Dict[str, Any]] = []
    any_file_denied = False
    for pattern in paths:
        state = states[pattern]
        status = state["status"]
        file_entries: List[Dict[str, Any]] = []
        access_failed = False
        file_denied = False
        for path, st in state["stats"]:
            entry: Dict[str, Any] = {
                "path": path,
                "device": int(st.st_dev),
                "inode": int(st.st_ino),
                "size": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
            }
            if status in (None, "ok", "quiet"):
                if sysroot == "/":
                    readable = measured_paths.get(path)
                else:
                    readable = _mode_access(
                        st, alloy[0], alloy[1], 0o4
                    ) if alloy is not None else None
                if readable is None:
                    access_failed = True
                else:
                    entry["accessible"] = bool(readable)
                    if not readable:
                        file_denied = True
            file_entries.append(entry)
        any_file_denied = any_file_denied or file_denied
        if status is None:
            if access_failed:
                status = "unavailable"
            elif state["matched"] == 0:
                status = "quiet"
            else:
                status = "ok"
        elif status in ("ok", "quiet") and access_failed:
            status = "unavailable"
        matches.append(
            {
                "pattern": pattern,
                "matched": int(state["matched"]),
                "skipped": 0,
                "truncated": status == "partial",
                "status": status,
                "error": state["error"],
                "files": file_entries,
            }
        )

    source_status = _source_rollup(
        root_records,
        matches,
        exact_records,
        file_denied=any_file_denied,
        required=required,
    )
    return {
        "status": source_status,
        "truncated": bool(truncated),
        "roots": root_records,
        "exact_files": exact_records,
        "matches": matches,
    }


def _unavailable_source_facts(
    roots: Sequence[str],
    paths: Sequence[str],
    exact_files: Sequence[str],
    reason: str,
) -> Dict[str, Any]:
    root_records = {
        root: {
            "exists": False,
            "is_dir": False,
            "is_symlink": False,
            "device": None,
            "blocked_ancestor": False,
            "status": "unavailable",
            "error": reason,
        }
        for root in roots
    }
    exact_records = {
        path: {
            "exists": False,
            "is_symlink": False,
            "device": None,
            "inode": None,
            "size": None,
            "mtime_ns": None,
            "status": "unavailable",
            "error": reason,
        }
        for path in exact_files
    }
    matches = [
        {
            "pattern": pattern,
            "matched": 0,
            "skipped": 0,
            "truncated": False,
            "status": "unavailable",
            "error": reason,
            "files": [],
        }
        for pattern in paths
    ]
    return {
        "status": "unavailable",
        "truncated": False,
        "roots": root_records,
        "exact_files": exact_records,
        "matches": matches,
    }


def _partial_source_facts(
    roots: Sequence[str], paths: Sequence[str], exact_files: Sequence[str]
) -> Dict[str, Any]:
    root_records = {
        root: {
            "exists": False,
            "is_dir": False,
            "is_symlink": False,
            "device": None,
            "blocked_ancestor": False,
            "status": "partial",
            "error": "logging evidence budget exhausted",
        }
        for root in roots
    }
    exact_records = {
        path: {
            "exists": False,
            "is_symlink": False,
            "device": None,
            "inode": None,
            "size": None,
            "mtime_ns": None,
            "status": "partial",
            "error": "logging evidence budget exhausted",
        }
        for path in exact_files
    }
    matches = [
        {
            "pattern": pattern,
            "matched": 0,
            "skipped": 0,
            "truncated": True,
            "status": "partial",
            "error": "logging evidence budget exhausted",
            "files": [],
        }
        for pattern in paths
    ]
    return {
        "status": "partial",
        "truncated": True,
        "roots": root_records,
        "exact_files": exact_records,
        "matches": matches,
    }


def _source_rollup(
    root_records: Dict[str, Dict[str, Any]],
    matches: Sequence[Dict[str, Any]],
    exact_records: Dict[str, Dict[str, Any]],
    *,
    file_denied: bool,
    required: bool,
) -> str:
    root_statuses = [record["status"] for record in root_records.values()]
    match_statuses = [match["status"] for match in matches]
    exact_statuses = [record["status"] for record in exact_records.values()]
    all_statuses = root_statuses + match_statuses + exact_statuses
    if "failed" in all_statuses:
        return "failed"
    if "unavailable" in all_statuses:
        return "unavailable"
    if "partial" in all_statuses:
        return "partial"
    if "denied" in all_statuses or file_denied:
        return "denied"
    blocked_required = any(
        record["status"] == "missing_root" for record in root_records.values()
    ) and required
    if not blocked_required:
        blocked_required = any(
            record["status"] == "missing_root"
            and record.get("parent_traversable") is not True
            for record in exact_records.values()
        ) and required
    if blocked_required:
        return "failed"
    active = any(status == "ok" for status in root_statuses) or any(
        record.get("exists") for record in exact_records.values()
    )
    future = any(
        not record.get("exists")
        and record.get("parent_traversable") is True
        for record in exact_records.values()
    )
    if active or future:
        return "ok"
    return "missing_root"


def _response_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":")))
    except (TypeError, ValueError) as exc:
        raise HelperError("logging evidence is not JSON serializable") from exc


def _bounded_logging_response(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the returned evidence inside a bounded serialized response.

    Exceeding the budget is *not* an excuse to drop identity rows: every
    affected source becomes explicitly ``partial`` with empty identity lists,
    preserving the requested keys while refusing to claim complete ownership.
    """
    if _response_bytes(facts) <= _MAX_LOGGING_RESPONSE_BYTES:
        return facts
    reason = "logging evidence exceeded the bounded response budget"
    sources = facts.get("sources", {})
    for source in sources.values():
        if not isinstance(source, dict):
            continue
        for layout in source.get("layouts", {}).values():
            if not isinstance(layout, dict):
                continue
            layout["status"] = "partial"
            layout["truncated"] = True
            for match in layout.get("matches", []):
                if not isinstance(match, dict):
                    continue
                match["status"] = "partial"
                match["truncated"] = True
                match["matched"] = 0
                match["skipped"] = 0
                match["error"] = reason
                match["files"] = []
    if _response_bytes(facts) <= _MAX_LOGGING_RESPONSE_BYTES:
        return facts
    # Last-resort metadata minimisation: keep the requested keys and status
    # vocabulary, but no per-file details.  The source remains partial.
    for source in sources.values():
        if not isinstance(source, dict):
            continue
        for layout in source.get("layouts", {}).values():
            if not isinstance(layout, dict):
                continue
            for root in layout.get("roots", {}):
                layout["roots"][root] = {
                    "exists": False,
                    "is_dir": False,
                    "is_symlink": False,
                    "device": None,
                    "blocked_ancestor": False,
                    "status": "partial",
                    "error": reason,
                }
            for path in layout.get("exact_files", {}):
                layout["exact_files"][path] = {
                    "exists": False,
                    "is_symlink": False,
                    "device": None,
                    "inode": None,
                    "size": None,
                    "mtime_ns": None,
                    "status": "partial",
                    "error": reason,
                }
    if _response_bytes(facts) > _MAX_LOGGING_RESPONSE_BYTES:
        raise HelperError(
            "logging evidence exceeds the bounded response budget",
            detail={"limit": _MAX_LOGGING_RESPONSE_BYTES},
        )
    return facts


def _logging_facts(
    request: Dict[str, Any],
    *,
    guest: Dict[str, Any],
    sysroot: str,
    runner: Runner,
) -> Dict[str, Any]:
    raw = request.get("logging")
    if raw is None:
        return _empty_logging_facts()
    parsed = _parse_logging_request(raw)
    if not guest.get("is_running") or guest.get("is_template"):
        return _empty_logging_facts()
    alloy = _lookup_user(sysroot, "alloy")
    try:
        sysroot_dev: Optional[int] = int(os.stat(sysroot).st_dev)
    except OSError:
        sysroot_dev = None
    mounts = _read_mountpoints(sysroot)
    clock = time.monotonic
    deadline = clock() + _MAX_LOGGING_SECONDS
    unit_cache: Dict[str, Dict[str, Any]] = {}
    plugins: Dict[str, Any] = {}
    for owner_id, entry in parsed["plugins"].items():
        units = {
            key: _unit_evidence(
                unit, unit_cache, sysroot=sysroot, runner=runner
            )
            for key, unit in entry["units"].items()
        }
        includes = {
            dependency_id: {
                "units": {
                    key: _unit_evidence(
                        unit, unit_cache, sysroot=sysroot, runner=runner
                    )
                    for key, unit in dependency["units"].items()
                }
            }
            for dependency_id, dependency in entry["includes"].items()
        }
        plugins[owner_id] = {"units": units, "includes": includes}
    sources: Dict[str, Any] = {}
    for key, entry in parsed["sources"].items():
        layouts: Dict[str, Any] = {}
        for layout_id, spec in entry["layouts"].items():
            layouts[layout_id] = _probe_source_layout(
                spec,
                sysroot=sysroot,
                alloy=alloy,
                runner=runner,
                clock=clock,
                deadline=deadline,
                sysroot_dev=sysroot_dev,
                mounts=mounts,
            )
        sources[key] = {"layouts": layouts}
    return _bounded_logging_response({"plugins": plugins, "sources": sources})


# --------------------------------------------------------------------------- #
# Scoped logging ACL repair (descriptor-anchored)
# --------------------------------------------------------------------------- #
#
# ``setfacl``/``getfacl`` have no descriptor API, so every mutation names an
# inherited descriptor through ``/proc/self/fd/N``: the child operates on the
# verified inode itself, never on a pathname the application account could have
# replaced with a symlink between measurement and repair.  Only a bounded batch
# of descriptors is handed to one process, and the parent keeps each descriptor
# open until that batch returns.  Nothing here chmods, chowns, reads log bodies,
# runs a shell, or accepts a caller-supplied command.

_ACL_USER = "alloy"
_MAX_ACL_ROOTS = 256
_MAX_ACL_EXACT_FILES = 256
_MAX_ACL_TARGETS = 512
_MAX_ACL_BATCH = 64
_MAX_ACL_ENTRIES = _MAX_LOGGING_FILES
_MAX_ACL_SECONDS = 300.0
_MAX_ACL_TOOL_SECONDS = 60.0
_MAX_ACL_TOOL_OUTPUT = 1024 * 1024
_MAX_ACL_REASON_CHARS = _MAX_LOGGING_ERROR_CHARS

# kind -> steady-state ACL text.  Files inside a dedicated root also use ``X``
# (execute only when the file already carries an execute bit), matching the
# previous guarded ``find -type f -exec setfacl -m u:alloy:rX`` scope.
_ACL_SPECS: Dict[str, str] = {
    "ancestor": "u:{uid}:x",
    "dir": "u:{uid}:rX,d:u:{uid}:r-x",
    "file": "u:{uid}:rX",
    "exact": "u:{uid}:r",
}


class _AclFailure(Exception):
    """A bounded, non-fatal ACL repair refusal returned as ``status=failed``."""


def _acl_result() -> Dict[str, Any]:
    return {
        "status": "complete",
        "changed": False,
        "repaired_roots": 0,
        "repaired_files": 0,
        "skipped_missing": 0,
        "error_count": 0,
        "reason": "",
    }


def _acl_fail(
    result: Dict[str, Any], reason: str, *, errors: int = 1
) -> Dict[str, Any]:
    result["status"] = "failed"
    result["error_count"] = max(1, int(result["error_count"]) + max(0, int(errors)))
    result["reason"] = _logging_error(reason)
    return result


def _acl_complete_reason(result: Dict[str, Any]) -> str:
    text = (
        "logging ACL grants applied for {0} root(s) and {1} exact file(s); "
        "{2} declared path(s) are absent"
    ).format(
        result["repaired_roots"],
        result["repaired_files"],
        result["skipped_missing"],
    )
    return text[: max(1, _MAX_ACL_REASON_CHARS)]


def _validate_logging_acl_request(request: object) -> Tuple[List[str], List[str]]:
    if not isinstance(request, dict):
        raise HelperError("logging_acl request must be an object")
    extra = sorted(set(request) - {"roots", "exact_files"})
    if extra:
        raise HelperError(
            "logging_acl request has unsupported fields",
            detail={"fields": extra[:8]},
        )
    missing = [key for key in ("roots", "exact_files") if key not in request]
    if missing:
        raise HelperError(
            "logging_acl request requires explicit roots and exact_files lists",
            detail={"fields": missing},
        )
    roots = _logging_path_list(
        request.get("roots"),
        label="roots",
        allow_glob=False,
        limit=_MAX_ACL_ROOTS,
        allow_duplicates=True,
    )
    exact_files = _logging_path_list(
        request.get("exact_files"),
        label="exact_files",
        allow_glob=False,
        limit=_MAX_ACL_EXACT_FILES,
        allow_duplicates=True,
    )
    if len(roots) + len(exact_files) > _MAX_ACL_TARGETS:
        raise HelperError("logging_acl request exceeds the bounded target limit")
    return roots, exact_files


def _fd_mount_id(fd: int) -> Optional[int]:
    """The mount ID of an open descriptor, or ``None`` when unmeasurable."""
    try:
        data = _read_bytes_optional("/proc/self/fdinfo/{0}".format(fd), 64 * 1024)
    except HelperError:
        return None
    if data is None:
        return None
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith("mnt_id:"):
            value = line.split(":", 1)[1].strip()
            return int(value) if value.isdigit() else None
    return None


def _parse_acl_sections(
    text: str, *, expected: int
) -> List[Dict[Tuple[str, str, str], str]]:
    sections: List[Dict[Tuple[str, str, str], str]] = []
    current: Optional[Dict[Tuple[str, str, str], str]] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# file: "):
            current = {}
            sections.append(current)
            continue
        if line.startswith("#"):
            continue
        if current is None:
            raise ValueError("ACL entry outside a file section")
        # ``getfacl`` appends a ``#effective:...`` comment when the mask limits
        # a named entry; it is a display annotation, not ACL data.
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        default = False
        if body.startswith("default:"):
            default = True
            body = body[len("default:") :]
        parts = body.split(":")
        if len(parts) != 3:
            raise ValueError("malformed ACL entry")
        tag, qualifier, perms = parts
        if len(perms) != 3 or any(ch not in "rwx-" for ch in perms):
            raise ValueError("malformed ACL permissions")
        current[("default" if default else "access", tag, qualifier)] = perms
    if len(sections) != expected:
        raise ValueError("ACL evidence does not match the requested targets")
    return sections


def _acl_has(
    grants: Dict[Tuple[str, str, str], str],
    uid: int,
    flags: str,
    *,
    default: bool = False,
) -> bool:
    scope = "default" if default else "access"
    perms = grants.get((scope, "user", str(uid)))
    mask = grants.get((scope, "mask", ""))
    if perms is None or mask is None:
        return False
    return all(flag in perms and flag in mask for flag in flags)


def _acl_file_flags(st: os.stat_result) -> str:
    return "rx" if (st.st_mode & 0o111) else "r"


def _acl_satisfied(
    grants: Dict[Tuple[str, str, str], str],
    kind: str,
    st: os.stat_result,
    uid: int,
) -> bool:
    if kind == "ancestor":
        return _acl_has(grants, uid, "x")
    if kind == "dir":
        return _acl_has(grants, uid, "rx") and _acl_has(
            grants, uid, "rx", default=True
        )
    if kind == "file":
        return _acl_has(grants, uid, _acl_file_flags(st))
    return _acl_has(grants, uid, "r")


class _AclRepair:
    """Bounded descriptor batches for one verified ACL repair pass."""

    def __init__(
        self,
        *,
        setfacl: str,
        getfacl: str,
        uid: int,
        result: Dict[str, Any],
        clock: Callable[[], float],
        deadline: float,
    ) -> None:
        self.setfacl = setfacl
        self.getfacl = getfacl
        self.uid = uid
        self.result = result
        self.clock = clock
        self.deadline = deadline
        self.changed = False
        self.batches: Dict[str, List[Tuple[int, os.stat_result, Optional[str]]]] = {
            kind: [] for kind in _ACL_SPECS
        }
        self.seen: Dict[str, Set[Tuple[int, int]]] = {
            kind: set() for kind in _ACL_SPECS
        }

    def add(
        self,
        kind: str,
        fd: int,
        st: os.stat_result,
        *,
        count: Optional[str] = None,
    ) -> None:
        """Take ownership of ``fd`` and flush the batch when it is full."""
        ident = (int(st.st_dev), int(st.st_ino))
        if ident in self.seen[kind]:
            os.close(fd)
            return
        if len(self.seen[kind]) >= _MAX_ACL_ENTRIES:
            os.close(fd)
            raise _AclFailure("a log ACL target set exceeds the bounded repair limit")
        self.seen[kind].add(ident)
        batch = self.batches[kind]
        batch.append((fd, st, count))
        if len(batch) >= _MAX_ACL_BATCH:
            self.flush(kind)

    def flush(self, kind: str) -> None:
        batch = self.batches[kind]
        if not batch:
            return
        self.batches[kind] = []
        try:
            self._apply(kind, batch)
        finally:
            for fd, _st, _count in batch:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def finish(self) -> None:
        for kind in ("ancestor", "dir", "file", "exact"):
            self.flush(kind)

    def close(self) -> None:
        for kind in list(self.batches):
            for fd, _st, _count in self.batches[kind]:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self.batches[kind] = []

    def _run(
        self, argv: Sequence[str], fds: Sequence[int]
    ) -> Tuple[int, bytes, bytes]:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise _AclFailure("ACL repair exceeded the bounded time budget")
        try:
            return _run_bounded_process(
                argv,
                stdin_bytes=b"",
                timeout_seconds=min(_MAX_ACL_TOOL_SECONDS, max(0.25, remaining)),
                out_limit=_MAX_ACL_TOOL_OUTPUT,
                err_limit=64 * 1024,
                pass_fds=fds,
            )
        except HelperError as exc:
            raise _AclFailure("the guest ACL tools failed") from exc

    def _read_grants(
        self, fds: Sequence[int]
    ) -> Dict[int, Dict[Tuple[str, str, str], str]]:
        argv = [self.getfacl, "-n", "-p", "--"] + [
            "/proc/self/fd/{0}".format(fd) for fd in fds
        ]
        rc, out, _err = self._run(argv, fds)
        if rc != 0:
            raise _AclFailure("failed to read the existing logging ACL")
        try:
            sections = _parse_acl_sections(
                out.decode("utf-8", "replace"), expected=len(fds)
            )
        except ValueError as exc:
            raise _AclFailure("the guest returned unreadable ACL evidence") from exc
        return {fd: section for fd, section in zip(fds, sections)}

    def _write_grants(self, fds: Sequence[int], kind: str) -> None:
        argv = [
            self.setfacl,
            "-m",
            _ACL_SPECS[kind].format(uid=self.uid),
            "--",
        ] + ["/proc/self/fd/{0}".format(fd) for fd in fds]
        rc, _out, _err = self._run(argv, fds)
        if rc != 0:
            raise _AclFailure("failed to update the logging ACL")

    def _apply(
        self, kind: str, batch: Sequence[Tuple[int, os.stat_result, Optional[str]]]
    ) -> None:
        if self.clock() > self.deadline:
            raise _AclFailure("ACL repair exceeded the bounded time budget")
        fds = [fd for fd, _st, _count in batch]
        current = self._read_grants(fds)
        needing = [
            fd
            for fd, st, _count in batch
            if not _acl_satisfied(current.get(fd, {}), kind, st, self.uid)
        ]
        if needing:
            self._write_grants(needing, kind)
            self.changed = True
        for _fd, _st, count in batch:
            if count:
                self.result[count] += 1


def _acl_ancestors(paths: Sequence[str]) -> List[str]:
    ordered: List[str] = []
    seen: Set[str] = set()
    for path in paths:
        current = path.rsplit("/", 1)[0]
        while current and current != "/":
            if current not in seen:
                seen.add(current)
                ordered.append(current)
            current = current.rsplit("/", 1)[0] if "/" in current else ""
    return sorted(ordered, key=lambda item: (item.count("/"), item))


def _acl_repair_ancestors(
    roots: Sequence[str],
    exact_files: Sequence[str],
    *,
    sysroot: str,
    repair: _AclRepair,
) -> None:
    for path in _acl_ancestors(list(roots) + list(exact_files)):
        if repair.clock() > repair.deadline:
            raise _AclFailure("ACL repair exceeded the bounded time budget")
        try:
            fd = _open_dir_path(_join(sysroot, path))
        except HelperError as exc:
            if exc.detail.get("errno") == errno.ENOENT:
                continue
            raise _AclFailure("a logging target has an unsafe ancestor") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode):
                raise _AclFailure("a logging target has an unsafe ancestor")
            owned = fd
            fd = -1
            repair.add("ancestor", owned, st)
        finally:
            if fd >= 0:
                os.close(fd)
    repair.flush("ancestor")


def _acl_walk_root(
    root_fd: int,
    root: str,
    root_dev: int,
    root_mnt: int,
    mounts: Set[str],
    repair: _AclRepair,
    budget: List[int],
) -> None:
    def scan(dir_fd: int, guest: str, depth: int) -> None:
        if depth > _MAX_LOGGING_DEPTH:
            raise _AclFailure("a log root exceeds the bounded traversal depth")
        try:
            with os.scandir(dir_fd) as iterator:
                for entry in iterator:
                    budget[0] -= 1
                    if budget[0] < 0:
                        raise _AclFailure(
                            "a log root exceeds the bounded file repair limit"
                        )
                    if repair.clock() > repair.deadline:
                        raise _AclFailure(
                            "ACL repair exceeded the bounded time budget"
                        )
                    path = guest.rstrip("/") + "/" + entry.name
                    if entry.name == QUARANTINE_DIRNAME:
                        continue
                    if path in mounts:
                        # Prune every declared mountpoint before even statting
                        # it: a dead network mount must not block the repair.
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise _AclFailure(
                            "failed to inspect a file beneath a log root"
                        ) from exc
                    mode = st.st_mode
                    if stat.S_ISLNK(mode):
                        # Never traverse or modify a symlink target; the
                        # previous find -P scope skipped them the same way.
                        continue
                    if stat.S_ISDIR(mode):
                        try:
                            child = os.open(
                                entry.name,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=dir_fd,
                            )
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                                continue
                            raise _AclFailure(
                                "cannot traverse a directory beneath a log root"
                            ) from exc
                        try:
                            child_st = os.fstat(child)
                            if not stat.S_ISDIR(child_st.st_mode):
                                continue
                            if int(child_st.st_dev) != root_dev:
                                continue
                            child_mnt = _fd_mount_id(child)
                            if child_mnt is None:
                                raise _AclFailure(
                                    "the guest mount identity is unavailable"
                                )
                            if child_mnt != root_mnt:
                                # A mount appeared after the table snapshot:
                                # never cross it, even on the same device.
                                continue
                            scan(child, path, depth + 1)
                            owned = child
                            child = -1
                            repair.add("dir", owned, child_st)
                        finally:
                            if child >= 0:
                                os.close(child)
                        continue
                    if not stat.S_ISREG(mode):
                        continue
                    try:
                        fd = os.open(
                            entry.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=dir_fd
                        )
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                            continue
                        raise _AclFailure(
                            "cannot open a file beneath a log root"
                        ) from exc
                    try:
                        file_st = os.fstat(fd)
                        if not stat.S_ISREG(file_st.st_mode):
                            continue
                        if int(file_st.st_dev) != root_dev:
                            continue
                        file_mnt = _fd_mount_id(fd)
                        if file_mnt is None:
                            raise _AclFailure(
                                "the guest mount identity is unavailable"
                            )
                        if file_mnt != root_mnt:
                            continue
                        owned = fd
                        fd = -1
                        repair.add("file", owned, file_st)
                    finally:
                        if fd >= 0:
                            os.close(fd)
        except OSError as exc:
            raise _AclFailure(
                "failed to enumerate a directory beneath a log root"
            ) from exc

    scan(root_fd, root, 0)


def _acl_repair_root(
    path: str,
    *,
    sysroot: str,
    mounts: Set[str],
    repair: _AclRepair,
    result: Dict[str, Any],
) -> None:
    if repair.clock() > repair.deadline:
        raise _AclFailure("ACL repair exceeded the bounded time budget")
    try:
        root_fd = _open_dir_path(_join(sysroot, path))
    except HelperError as exc:
        if exc.detail.get("errno") == errno.ENOENT:
            result["skipped_missing"] += 1
            return
        raise _AclFailure("a declared log root is not a safe directory") from exc
    try:
        st = os.fstat(root_fd)
        if not stat.S_ISDIR(st.st_mode):
            raise _AclFailure("a declared log root is not a directory")
        root_mnt = _fd_mount_id(root_fd)
        if root_mnt is None:
            raise _AclFailure("the guest mount identity is unavailable")
        budget = [_MAX_ACL_ENTRIES]
        _acl_walk_root(
            root_fd, path, int(st.st_dev), root_mnt, mounts, repair, budget
        )
        owned = root_fd
        root_fd = -1
        repair.add("dir", owned, st)
        repair.flush("dir")
        repair.flush("file")
        result["repaired_roots"] += 1
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _acl_repair_exact(
    path: str,
    *,
    sysroot: str,
    mounts: Set[str],
    repair: _AclRepair,
    result: Dict[str, Any],
) -> None:
    if repair.clock() > repair.deadline:
        raise _AclFailure("ACL repair exceeded the bounded time budget")
    parent, basename = path.rsplit("/", 1)
    parent = parent or "/"
    try:
        parent_fd = _open_dir_path(_join(sysroot, parent))
    except HelperError as exc:
        if exc.detail.get("errno") == errno.ENOENT:
            # The declared file and its parent are genuinely absent; other
            # existing ancestors were already granted traverse-only access.
            result["skipped_missing"] += 1
            return
        raise _AclFailure("a declared exact log file has an unsafe parent") from exc
    try:
        if _behind_mount(path, mounts):
            # Reject a known file/ancestor mount before statting a possibly
            # dead network mount's root.
            raise _AclFailure("a declared exact log file is behind a mount point")
        try:
            observed = os.stat(
                basename, dir_fd=parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            observed = None
        except OSError as exc:
            raise _AclFailure(
                "failed to inspect a declared exact log file"
            ) from exc
        if observed is None:
            result["skipped_missing"] += 1
            return
        if stat.S_ISLNK(observed.st_mode):
            raise _AclFailure("a declared exact log file is a symbolic link")
        if not stat.S_ISREG(observed.st_mode):
            raise _AclFailure("a declared exact log file is not a regular file")
        try:
            fd = os.open(basename, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError:
            result["skipped_missing"] += 1
            return
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise _AclFailure(
                    "a declared exact log file changed to a symbolic link"
                ) from exc
            raise _AclFailure(
                "cannot open a declared exact log file"
            ) from exc
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or (int(st.st_dev), int(st.st_ino))
                != (int(observed.st_dev), int(observed.st_ino))
            ):
                raise _AclFailure(
                    "a declared exact log file changed during repair"
                )
            if _fd_mount_id(fd) is None:
                raise _AclFailure("the guest mount identity is unavailable")
            owned = fd
            fd = -1
            repair.add("exact", owned, st, count="repaired_files")
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(parent_fd)


def logging_acl(request: Dict[str, Any], *, sysroot: str = "/") -> Dict[str, Any]:
    """Grant ``alloy`` scoped, descriptor-verified access to resolved sources.

    ``roots`` are dedicated log directories: each ancestor up to (but never
    including) ``/`` gains traverse-only ``x``; the root and its descendants
    gain ``rX`` plus an inheritable default ``r-x``, walking without following
    symlinks, staying on the root's mount/device, and pruning the quarantine
    tree and every nested mount.  ``exact_files`` gain read access on that
    verified inode only, with traverse-only access on their parents (never a
    recursive/default grant on application data).  Declared paths that are
    genuinely absent are skipped safely; unsafe, ambiguous or unmeasurable
    targets fail the whole result rather than being silently ignored.  No
    ``chmod``/``chown`` is ever issued and the caller's command text is never
    executed.
    """
    roots, exact_files = _validate_logging_acl_request(request)
    result = _acl_result()
    if not roots and not exact_files:
        result["reason"] = "no logging ACL targets were requested"
        return result
    alloy = _lookup_user(sysroot, _ACL_USER)
    if alloy is None:
        return _acl_fail(result, "the alloy user is not defined in the guest")
    setfacl = _find_binary(sysroot, "setfacl")
    getfacl = _find_binary(sysroot, "getfacl")
    if setfacl is None or getfacl is None:
        return _acl_fail(result, "the guest ACL tools are unavailable")
    mounts = _read_mountpoints(sysroot)
    if mounts is None:
        return _acl_fail(result, "the guest mount table is unavailable")
    clock = time.monotonic
    repair = _AclRepair(
        setfacl=setfacl,
        getfacl=getfacl,
        uid=alloy[0],
        result=result,
        clock=clock,
        deadline=clock() + _MAX_ACL_SECONDS,
    )
    try:
        _acl_repair_ancestors(
            roots, exact_files, sysroot=sysroot, repair=repair
        )
        for path in roots:
            _acl_repair_root(
                path,
                sysroot=sysroot,
                mounts=mounts,
                repair=repair,
                result=result,
            )
        for path in exact_files:
            _acl_repair_exact(
                path,
                sysroot=sysroot,
                mounts=mounts,
                repair=repair,
                result=result,
            )
        repair.finish()
    except _AclFailure as exc:
        return _acl_fail(result, str(exc))
    finally:
        repair.close()
    result["changed"] = repair.changed
    result["reason"] = _acl_complete_reason(result)
    return result


_DEAD_TASK_STATES = frozenset({"Z", "X", "x"})


def _proc_task_state(pid_dir: str, tid: str) -> Optional[str]:
    """Return the state from ``<pid_dir>/stat``, or ``None`` if the task is gone.

    ``/proc/<pid>/stat`` stays mode 0444 even for a non-dumpable task whose
    ``/proc/<pid>/fd`` directory is root-owned and denied, so it remains the
    liveness source when descriptor enumeration is refused.  ``""`` means the
    state could not be read, parsed, or identified: callers must fail closed.
    """
    try:
        with open(os.path.join(pid_dir, "stat"), "rb") as handle:
            raw = handle.read(4096)
    except FileNotFoundError:
        return None
    except OSError:
        return ""
    end = raw.rfind(b")")
    if end < 0:
        return ""
    if raw[:end].split(b" ", 1)[0] != tid.encode():
        return ""
    fields = raw[end + 1:].split()
    if not fields or len(fields[0]) != 1:
        return ""
    return chr(fields[0][0])


def _departed_process(pid_dir: str, pid: str) -> bool:
    """True only when ``pid`` provably no longer holds any file descriptor.

    A denied ``/proc/<pid>/fd`` listing is *not* proof of absence: a live,
    non-dumpable process keeps a descriptor table we cannot read, so permission
    denial must never be treated as "no writers".  The kernel releases a task's
    file table during ``do_exit`` before the zombie state, so a fully reaped PID
    -- or a thread group whose every thread is itself dead -- cannot reference a
    live writer.  A live leader, a surviving sibling thread, an unreadable or
    unidentifiable state, and an incomplete ``task`` enumeration all return
    ``False`` so the caller keeps failing closed.
    """
    state = _proc_task_state(pid_dir, pid)
    if state is None:
        return True
    if state not in _DEAD_TASK_STATES:
        return False
    try:
        threads = _listdir(os.path.join(pid_dir, "task"))
    except HelperError:
        return False
    if not threads or pid not in threads:
        return False
    for tid in threads:
        if not tid.isdigit():
            return False
        tstate = _proc_task_state(os.path.join(pid_dir, "task", tid), tid)
        if tstate not in _DEAD_TASK_STATES:
            return False
    return True


def _scan_proc(proc_root: str) -> Tuple[Set[Tuple[int, int]], List[str]]:
    """Return (writable file inodes, busy tool names) for a guest ``/proc``."""
    names = _listdir(proc_root)
    if names is None:
        raise HelperError(
            "failed to enumerate the guest /proc tree", detail={"path": proc_root}
        )
    pids = [n for n in names if n.isdigit()]
    if not pids:
        raise HelperError(
            "no processes are visible; refusing to trust writer state",
            detail={"path": proc_root},
        )
    writable: Set[Tuple[int, int]] = set()
    busy: Set[str] = set()
    for pid in pids:
        pid_dir = os.path.join(proc_root, pid)
        cmdline = _read_bytes_optional(os.path.join(pid_dir, "cmdline"), 64 * 1024)
        if cmdline:
            busy |= _classify_busy(cmdline)
        fd_dir = os.path.join(pid_dir, "fd")
        try:
            fds = os.listdir(fd_dir)
        except FileNotFoundError:
            continue
        except OSError as exc:
            if _departed_process(pid_dir, pid):
                # The task exited while we scanned it: the kernel already
                # released its file table, so a refused descriptor listing
                # cannot hide a live writer and this PID is safe to skip.
                continue
            raise HelperError(
                f"cannot enumerate {fd_dir}: {exc.strerror or exc}; run as root",
                detail={
                    "path": fd_dir,
                    "task_state": _proc_task_state(pid_dir, pid),
                },
            )
        if (
            not fds
            and _proc_task_state(pid_dir, pid) in _DEAD_TASK_STATES
            and not _departed_process(pid_dir, pid)
        ):
            # An exited leader can expose an empty fd table while surviving
            # threads still write through their own tables.
            raise HelperError(
                "cannot trust an empty descriptor table for an incompletely exited process",
                detail={"path": fd_dir},
            )
        for fd_name in fds:
            fd_path = os.path.join(fd_dir, fd_name)
            try:
                st = os.stat(fd_path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise HelperError(
                    f"cannot inspect {fd_path}: {exc.strerror or exc}; run as root",
                    detail={"path": fd_path},
                )
            if not stat.S_ISREG(st.st_mode):
                continue
            if _fd_writable(os.path.join(pid_dir, "fdinfo", fd_name)):
                writable.add((int(st.st_dev), int(st.st_ino)))
    return writable, sorted(busy)


def _fd_writable(fdinfo_path: str) -> bool:
    data = _read_bytes_optional(fdinfo_path, 4096)
    if data is None:
        # The fdinfo entry may vanish as the process exits; be conservative.
        return True
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith("flags:"):
            raw = line.split(":", 1)[1].strip().split()[0]
            try:
                flags = int(raw, 8)
            except ValueError:
                return True
            return (flags & 0o3) in (1, 2)
    return True


_BUN_IDLE_FLAGS = frozenset(
    {"-h", "--help", "-v", "-V", "--version", "--revision", "help"}
)


def _bun_busy(args: Sequence[str]) -> Set[str]:
    """Classify a Bun argv: a runtime entrypoint is harmless, mutations are not.

    Only a genuine standalone help/version invocation (a single help/version
    token) is idle.  A later ``-v``/``--help`` flag never exempts a mutation
    or build shape: ``bun install -v`` and ``bun run build --help`` are still
    installing/building and must hold the cache-preserving build gate.
    """
    if len(args) == 1 and args[0] in _BUN_IDLE_FLAGS:
        return set()
    if not args:
        return {"bun"}
    first = args[0]
    if first == "pm" and len(args) >= 2 and args[1] == "cache":
        # The manager's own bounded path query; prune/rm are mutations.
        return set() if len(args) == 2 or args[2] == "path" else {"bun"}
    if first == "run" and len(args) >= 2:
        target = args[1]
        if target == "start" or target.endswith(_BUN_ENTRYPOINT_SUFFIXES):
            return set()
        if target in ("build", "install", "rebuild"):
            return {"bun", "build"}
        return {"bun"}
    if first.endswith(_BUN_ENTRYPOINT_SUFFIXES):
        return set()
    if first == "build":
        return {"bun", "build"}
    # Unknown/flags-first/package shapes stay conservatively busy.
    return {"bun"}


def _classify_busy(argv: bytes) -> Set[str]:
    parts = [p.decode("utf-8", "replace") for p in argv.split(b"\0") if p]
    if not parts:
        return set()
    prog = os.path.basename(parts[0])
    args = parts[1:]
    out: Set[str] = set()
    for tool, names in BUSY_PROGRAMS.items():
        if prog in names:
            out.add(tool)
    if prog in ("npm", "npx", "yarn", "pnpm") and any(
        a in ("build", "install", "run", "rebuild") for a in args
    ):
        out.add("build")
    if prog == "node" and any(
        a.endswith(("webpack", "vite", "rollup", "esbuild", "tsc", "node-gyp")) for a in args
    ):
        out.add("build")
    if prog == "bun":
        out |= _bun_busy(args)
    return out


def _lock_holder_tools(sysroot: str, writable: Set[Tuple[int, int]]) -> Set[str]:
    """Tools holding a known APT/dpkg lock, by read-only inode comparison.

    A nonstandard or wrapped package process still opens the well-known lock
    file, so matching its device/inode against the writable-fd set catches it.
    Missing lock files are normal (nothing is running); the files are only ever
    lstat-ed, never opened, created or removed.
    """
    busy: Set[str] = set()
    for guest_path, tools in PACKAGE_LOCK_FILES:
        try:
            st = os.lstat(_join(sysroot, guest_path))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HelperError(
                f"cannot stat the package lock {guest_path}: {exc.strerror or exc}",
                detail={"path": guest_path},
            )
        if stat.S_ISREG(st.st_mode) and (int(st.st_dev), int(st.st_ino)) in writable:
            busy.update(tools)
    return busy


def _pbs_active_tasks(sysroot: str) -> Set[str]:
    path = _join(sysroot, PBS_TASK_ROOT + "/active")
    data = _read_bytes_optional(path, MAX_ACTIVE_INDEX_BYTES)
    if data is None:
        return set()
    return set(UPID_RE.findall(data.decode("utf-8", "replace")))


def _discover_npm(
    sysroot: str, writer: Set[Tuple[int, int]]
) -> Tuple[List[Dict[str, Any]], Set[str]]:
    files: List[Dict[str, Any]] = []
    dirs: Set[str] = set()
    root = _join(sysroot, NPM_LOG_ROOT)
    if not os.path.isdir(root):
        return files, dirs
    for dirpath, dirnames, filenames in _walk(root):
        dirs.add(_guest_rel(sysroot, dirpath))
        dirnames[:] = [d for d in sorted(dirnames) if d != QUARANTINE_DIRNAME]
        for name in sorted(filenames):
            if not NPM_LOG_RE.match(name):
                continue
            joined = os.path.join(dirpath, name)
            st = _lstat_strict(joined)
            if not stat.S_ISREG(st.st_mode):
                continue
            files.append(
                _wire_from_stat(
                    st,
                    path=_guest_rel(sysroot, joined),
                    compression=_compression_for(name),
                    profile="npm",
                    log_kind="application",
                    is_active=(int(st.st_dev), int(st.st_ino)) in writer,
                )
            )
    return files, dirs


def _discover_pbs_tasks(
    sysroot: str, writer: Set[Tuple[int, int]], active_tasks: Set[str]
) -> Tuple[List[Dict[str, Any]], Set[str]]:
    files: List[Dict[str, Any]] = []
    dirs: Set[str] = set()
    root = _join(sysroot, PBS_TASK_ROOT)
    names = _listdir(root)
    if names is None:
        return files, dirs
    dirs.add(PBS_TASK_ROOT)

    def add_file(joined: str, log_kind: str, *, active: bool) -> None:
        st = _lstat_strict(joined)
        if not stat.S_ISREG(st.st_mode):
            return
        files.append(
            _wire_from_stat(
                st,
                path=_guest_rel(sysroot, joined),
                compression=_compression_for(os.path.basename(joined)),
                profile="pbs",
                log_kind=log_kind,
                is_active=active or (int(st.st_dev), int(st.st_ino)) in writer,
            )
        )

    for name in names:
        joined = os.path.join(root, name)
        st = _lstat_strict(joined)
        if stat.S_ISDIR(st.st_mode):
            if HEX2_RE.match(name):
                dirs.add(_guest_rel(sysroot, joined))
                for sub in _listdir(joined) or []:
                    if not sub.startswith("UPID:"):
                        continue
                    add_file(
                        os.path.join(joined, sub),
                        "task",
                        active=sub in active_tasks,
                    )
            elif name == "archive":
                dirs.add(_guest_rel(sysroot, joined))
                for sub in _listdir(joined) or []:
                    add_file(os.path.join(joined, sub), "task_index", active=False)
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if name in PBS_CONTROL_NAMES or name.endswith(".lock"):
            continue
        if name == "archive" or name.startswith("archive."):
            add_file(joined, "task_index", active=False)
    return files, dirs


def _discover_pbs_api(
    sysroot: str, writer: Set[Tuple[int, int]]
) -> Tuple[List[Dict[str, Any]], Set[str]]:
    files: List[Dict[str, Any]] = []
    dirs: Set[str] = set()
    root = _join(sysroot, PBS_API_ROOT)
    names = _listdir(root)
    if names is None:
        return files, dirs
    dirs.add(PBS_API_ROOT)
    for name in names:
        if not PBS_API_RE.match(name):
            continue
        joined = os.path.join(root, name)
        st = _lstat_strict(joined)
        if not stat.S_ISREG(st.st_mode):
            continue
        is_current = name in ("access.log", "auth.log")
        files.append(
            _wire_from_stat(
                st,
                path=_guest_rel(sysroot, joined),
                compression=_compression_for(name),
                profile="pbs",
                log_kind="api",
                is_active=is_current and (int(st.st_dev), int(st.st_ino)) in writer,
            )
        )
    return files, dirs


def _cache_allowed(candidate: str, resolved_real: str, spec: _CacheSpec, sysroot: str) -> bool:
    for root in spec.roots:
        root_real = _join(sysroot, root)
        if resolved_real == root_real:
            return True
        if spec.child and resolved_real.startswith(root_real + "/"):
            rest = resolved_real[len(root_real) + 1:]
            if "/" not in rest and spec.child.match(rest):
                return True
    return False


def _tree_allocated_lenient(path: str) -> Tuple[Optional[int], Optional[str]]:
    try:
        return _tree_allocated(path), None
    except HelperError as exc:
        return None, str(exc)


def _bun_dir_open_flags() -> Optional[int]:
    """Directory-open flags that cannot refresh access times or follow links."""
    if not (
        hasattr(os, "O_NOATIME")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
    ):
        return None
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | os.O_NOATIME
        | getattr(os, "O_CLOEXEC", 0)
    )


def _bun_cache_usage(root: str) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """Measure Bun's cache without ever refreshing a package age.

    Returns ``(allocated_bytes, reclaimable_bytes, error)``.  Bun's native
    prune treats ``max(atime, mtime)`` as a package's age, so a plain
    ``os.walk``/``scandir`` would reset every directory's atime and prevent a
    stale prune.  Every directory is therefore opened relative to its verified
    parent with ``O_NOATIME|O_NOFOLLOW`` and only ``fstatat`` metadata is read;
    there is no fallback that touches an age.  ``allocated`` matches
    :func:`_tree_allocated` (all regular files, including retained ``links/``
    and hardlinks); ``reclaimable`` counts only unique regular files with no
    other hardlink outside the retained ``links/`` subtree, i.e. blocks a
    native prune can actually free.  Any enumeration failure returns
    ``(None, None, reason)`` rather than a fabricated zero.
    """
    flags = _bun_dir_open_flags()
    if flags is None:
        return (
            None,
            None,
            "this Python/kernel lacks O_NOATIME/O_DIRECTORY/O_NOFOLLOW; "
            "refusing to measure the Bun cache",
        )
    allocated = 0
    reclaimable = 0
    seen: Set[Tuple[int, int]] = set()

    def walk(fd: int, rel: str, depth: int, in_links: bool) -> None:
        nonlocal allocated, reclaimable
        label = rel or "."
        try:
            names = sorted(os.listdir(fd))
        except OSError as exc:
            raise HelperError(
                f"failed to enumerate {root}/{label}: {exc.strerror or exc}"
            )
        for name in names:
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                raise HelperError(
                    f"failed to stat {root}/{label}/{name}: {exc.strerror or exc}"
                )
            child_rel = f"{rel}/{name}" if rel else name
            child_links = in_links or child_rel == _BUN_LINKS_DIRNAME
            if stat.S_ISDIR(st.st_mode):
                if name == QUARANTINE_DIRNAME:
                    continue
                if depth >= _MAX_BUN_WALK_DEPTH:
                    raise HelperError(
                        f"Bun cache tree at {root}/{label} exceeds the "
                        f"{_MAX_BUN_WALK_DEPTH} level walk limit"
                    )
                try:
                    child_fd = os.open(name, flags, dir_fd=fd)
                except OSError as exc:
                    raise HelperError(
                        f"cannot open {root}/{child_rel} without updating "
                        f"access times: {exc.strerror or exc}"
                    )
                try:
                    walk(child_fd, child_rel, depth + 1, child_links)
                finally:
                    try:
                        os.close(child_fd)
                    except OSError:
                        pass
            elif stat.S_ISREG(st.st_mode):
                blocks = int(st.st_blocks) * 512
                allocated += blocks
                if not child_links and int(st.st_nlink) == 1:
                    identity = (int(st.st_dev), int(st.st_ino))
                    if identity not in seen:
                        seen.add(identity)
                        reclaimable += blocks

    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        return (
            None,
            None,
            f"cannot open {root} without updating access times: "
            f"{exc.strerror or exc}",
        )
    try:
        walk(root_fd, "", 0, False)
    except HelperError as exc:
        return None, None, str(exc)
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass
    return allocated, reclaimable, None


def _cache_is_dependency(sysroot: str, cache_real: str, *, tool: str) -> bool:
    cache = cache_real.rstrip("/")
    prefixes = {cache, cache + "/"}
    # Bun keeps global-store links under <cache>/links; native prune preserves
    # that subtree, so a dependency pointing there does not block pruning.  Any
    # other reference into the cache (or another tool's cache) still does.
    bun_links = cache + "/" + _BUN_LINKS_DIRNAME
    roots: List[str] = []
    for root in DEPENDENCY_ROOTS:
        roots.append(_join(sysroot, root))
    opt_root = _join(sysroot, "/opt")
    for name in _listdir(opt_root) or []:
        joined = os.path.join(opt_root, name)
        try:
            if os.path.isdir(joined):
                roots.append(os.path.join(joined, "node_modules"))
        except OSError:
            continue
    for root in roots:
        for sub in ("", ".bin", ".pnpm", ".bun"):
            directory = os.path.join(root, sub) if sub else root
            for name in _listdir(directory) or []:
                joined = os.path.join(directory, name)
                try:
                    if not os.path.islink(joined):
                        continue
                except OSError:
                    continue
                target = os.path.realpath(joined).rstrip("/") or "/"
                if tool == "bun" and (
                    target == bun_links or target.startswith(bun_links + "/")
                ):
                    continue
                if target in prefixes or target.startswith(cache + "/"):
                    return True
    return False


def _probe_caches(sysroot: str, runner: Runner) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for spec in CACHE_SPECS:
        reported = _run_first_line(runner, spec.command) if spec.command else None
        candidate: Optional[str] = None
        source: Optional[str] = None
        if reported and reported.startswith("/") and not _dotdot(reported):
            candidate = reported.rstrip("/") or "/"
            source = "tool"
        if candidate is None:
            for root in spec.roots:
                if os.path.isdir(_join(sysroot, root)):
                    candidate = root
                    source = "allowlist"
                    break
        entry: Dict[str, Any] = {
            "tool": spec.tool,
            "root": candidate,
            "resolved": None,
            "source": source,
            "exists": False,
            "allowlisted": False,
            "escaped": False,
            "allocated_bytes": 0,
            "version": _run_first_line(runner, [spec.tool, "--version"]) if spec.command else None,
            "dependency_referenced": False,
            "error": None,
        }
        if spec.tool == "bun":
            entry["binary"] = None
            entry["cache_prune_supported"] = False
            entry["reclaimable_bytes"] = 0
        if candidate is not None:
            joined = _join(sysroot, candidate)
            resolved = os.path.realpath(joined)
            entry["resolved"] = resolved
            entry["escaped"] = resolved != joined
            entry["exists"] = os.path.isdir(resolved)
            entry["allowlisted"] = not entry["escaped"] and _cache_allowed(candidate, resolved, spec, sysroot)
            entry["root_owned"] = entry["exists"] and os.stat(resolved).st_uid == 0
            if entry["exists"] and entry["allowlisted"]:
                if spec.tool == "bun":
                    allocated, reclaimable, error = _bun_cache_usage(resolved)
                    entry["allocated_bytes"] = allocated
                    entry["reclaimable_bytes"] = reclaimable
                    entry["error"] = error
                else:
                    allocated, error = _tree_allocated_lenient(resolved)
                    entry["allocated_bytes"] = allocated
                    entry["error"] = error
                try:
                    entry["dependency_referenced"] = _cache_is_dependency(
                        sysroot, resolved, tool=spec.tool
                    )
                except HelperError as exc:
                    entry["dependency_referenced"] = True
                    entry["error"] = entry["error"] or str(exc)
        if spec.tool == "bun":
            found = _find_binary(sysroot, "bun")
            entry["binary"] = _guest_rel(sysroot, found) if found else None
            entry["cache_prune_supported"] = bool(found) and _bun_prune_supported(runner)
        out[spec.tool] = entry
    return out


def _default_proc_root(sysroot: str) -> str:
    return _join(sysroot, "/proc")


def _disk_facts(sysroot: str) -> Dict[str, Any]:
    try:
        st = os.statvfs(sysroot)
    except OSError as exc:
        raise HelperError(
            f"failed to stat the filesystem for {sysroot}: {exc.strerror or exc}",
            detail={"path": sysroot},
        )
    frs = st.f_frsize or st.f_bsize
    total = frs * st.f_blocks
    available = frs * st.f_bavail
    used = frs * (st.f_blocks - st.f_bfree)
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_percent": round(used * 100.0 / total, 2) if total else 0.0,
    }


def probe(
    request: Dict[str, Any],
    *,
    sysroot: str = "/",
    proc_root: Optional[str] = None,
    runner: Optional[Runner] = None,
) -> Dict[str, Any]:
    """Read-only guest facts.  Raises on failed enumeration (never guesses)."""
    guest = _guest_facts(request.get("guest"), sysroot)
    runner = runner or _default_runner(sysroot)
    proc = proc_root or _default_proc_root(sysroot)
    evidence = _profile_evidence(sysroot)
    profiles = [p for p in PROFILES if evidence[p]["detected"]]
    binaries = {name: _find_binary(sysroot, name) for name in REQUIRED_BINARIES}
    busy: List[str] = []
    writer: Set[Tuple[int, int]] = set()
    if guest["is_running"]:
        writer, busy = _scan_proc(proc)
        locked = _lock_holder_tools(sysroot, writer)
        if locked:
            busy = sorted(set(busy) | locked)
    files: List[Dict[str, Any]] = []
    dirs: Set[str] = set()
    if "npm" in profiles:
        found, found_dirs = _discover_npm(sysroot, writer)
        files.extend(found)
        dirs |= found_dirs
    if "pbs" in profiles:
        active = _pbs_active_tasks(sysroot)
        found, found_dirs = _discover_pbs_tasks(sysroot, writer, active)
        files.extend(found)
        dirs |= found_dirs
        found, found_dirs = _discover_pbs_api(sysroot, writer)
        files.extend(found)
        dirs |= found_dirs
    files.sort(key=lambda entry: entry["path"])
    ready, detail = _log_access(sysroot, files, sorted(dirs), runner)
    return {
        "guest": guest,
        "disk": _disk_facts(sysroot),
        "journal_bytes": _tree_allocated(*(_join(sysroot, d) for d in JOURNAL_DIRS)),
        "profiles": profiles,
        "files": files,
        "cache_paths": _probe_caches(sysroot, runner),
        "busy_tools": busy,
        "binaries": binaries,
        "log_access_ready": ready,
        "log_access_detail": detail,
        "policy_sha256": _policy_hashes(sysroot),
        "profile_evidence": evidence,
        "logging": _logging_facts(
            request, guest=guest, sysroot=sysroot, runner=runner
        ),
    }


def _stopped_probe_facts(guest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "guest": guest,
        "disk": None,
        "journal_bytes": 0,
        "profiles": [],
        "files": [],
        "cache_paths": {},
        "busy_tools": [],
        "binaries": {},
        "log_access_ready": False,
        "log_access_detail": {"alloy_user": False, "dirs": {}, "files": {}},
        "policy_sha256": {name: "" for name in POLICY_FILES},
        "profile_evidence": {"npm": {}, "pbs": {}},
        "logging": _empty_logging_facts(),
    }


# --------------------------------------------------------------------------- #
# Root-filesystem directory usage (bounded, read-only diagnostics)
# --------------------------------------------------------------------------- #


class _DiskUnavailable(Exception):
    """The disk worker cannot establish a trustworthy metadata-only measurement."""


class _DiskRow:
    """One bounded wire directory row (unique-inode allocated blocks)."""

    __slots__ = ("path", "depth", "allocated", "direct")

    def __init__(self, path: str, depth: int) -> None:
        self.path = path
        self.depth = depth
        self.allocated = 0
        self.direct = 0


class _DiskFrame:
    """One active descriptor-anchored DFS frame (bounded by physical depth)."""

    __slots__ = ("fd", "path", "depth", "entries", "rows", "own_row")

    def __init__(
        self,
        fd: int,
        path: str,
        depth: int,
        entries: Any,
        rows: Tuple[_DiskRow, ...],
        own_row: Optional[_DiskRow],
    ) -> None:
        self.fd = fd
        self.path = path
        self.depth = depth
        self.entries = entries
        self.rows = rows
        self.own_row = own_row


def _disk_reason(text: object) -> str:
    cleaned = " ".join(str(text).split())
    cleaned = "".join(
        ch if ord(ch) >= 32 and ord(ch) != 127 else " " for ch in cleaned
    )
    return cleaned[:DISK_USAGE_MAX_REASON_CHARS]


def _disk_path_depth(path: str) -> int:
    if path == "/":
        return 0
    return len([part for part in path.split("/") if part])


def _disk_normalize_path(value: object, *, label: str) -> str:
    """A bounded normalized guest-absolute directory path (never the root)."""
    if not isinstance(value, str) or not value:
        raise HelperError(f"{label} must be a nonempty absolute path")
    if len(value.encode("utf-8")) > DISK_USAGE_MAX_PATH_BYTES:
        raise HelperError(f"{label} exceeds the bounded path length")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise HelperError(f"{label} must not contain control characters")
    if "://" in value or any(ch in value for ch in "*?[\\{}"):
        raise HelperError(f"{label} is not a supported absolute path")
    if not value.startswith("/") or value.startswith("//"):
        raise HelperError(f"{label} must be an absolute normalized path")
    parts = value.split("/")
    if any(part in (".", "..") for part in parts):
        raise HelperError(f"{label} must not contain traversal components")
    if any(part == "" for part in parts[1:]):
        raise HelperError(f"{label} must not contain repeated separators")
    if len(value) > 1 and value.endswith("/"):
        raise HelperError(f"{label} must not end with a slash")
    if value == "/":
        raise HelperError(f"{label} must not be the filesystem root")
    return value


def _validate_disk_usage_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the fixed disk-usage request; unknown/test-only fields fail."""
    if not isinstance(request, dict):
        raise HelperError("disk_usage request must be a JSON object")
    expected = {"max_depth", "max_entries", "timeout_seconds", "extra_paths"}
    unknown = sorted(set(request) - expected)
    if unknown:
        raise HelperError(
            "disk_usage request has unsupported fields",
            detail={"fields": unknown[:8]},
        )
    missing = sorted(expected - set(request))
    if missing:
        raise HelperError(
            "disk_usage request is missing required fields",
            detail={"fields": missing[:8]},
        )
    depth = request["max_depth"]
    if (
        isinstance(depth, bool)
        or not isinstance(depth, int)
        or not (0 <= depth <= DISK_USAGE_MAX_DEPTH)
    ):
        raise HelperError(
            "max_depth must be an integer within the report-depth limit",
            detail={"max_depth": depth if isinstance(depth, int) else None},
        )
    max_entries = request["max_entries"]
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or not (DISK_USAGE_MIN_ENTRIES <= max_entries <= DISK_USAGE_MAX_ENTRIES)
    ):
        raise HelperError(
            "max_entries must be an integer within the entry limit",
            detail={"max_entries": max_entries if isinstance(max_entries, int) else None},
        )
    timeout = request["timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise HelperError("timeout_seconds must be a number")
    timeout_seconds = float(timeout)
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > DISK_USAGE_MAX_SECONDS
    ):
        raise HelperError(
            "timeout_seconds must be finite and within the worker deadline",
        )
    raw_extras = request["extra_paths"]
    if not isinstance(raw_extras, list):
        raise HelperError("extra_paths must be a list")
    if len(raw_extras) > DISK_USAGE_MAX_EXTRA_PATHS:
        raise HelperError("extra_paths exceeds the bounded extra-path limit")
    extras: List[str] = []
    for index, item in enumerate(raw_extras):
        path = _disk_normalize_path(item, label=f"extra_paths[{index}]")
        if _disk_path_depth(path) > DISK_USAGE_MAX_EXTRA_DEPTH:
            raise HelperError(f"extra_paths[{index}] exceeds the bounded path depth")
        if path in extras:
            raise HelperError(f"extra_paths[{index}] repeats {path!r}")
        extras.append(path)
    return {
        "max_depth": depth,
        "max_entries": max_entries,
        "timeout_seconds": timeout_seconds,
        "extra_paths": tuple(sorted(extras)),
    }


def _disk_open_flags() -> Optional[Tuple[int, int]]:
    """Descriptor guards: O_PATH probe flags and an atime-preserving read."""
    if not (
        hasattr(os, "O_PATH")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_NOATIME")
    ):
        return None
    cloexec = getattr(os, "O_CLOEXEC", 0)
    path_flags = os.O_PATH | os.O_NOFOLLOW | cloexec
    read_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOATIME | cloexec
    return path_flags, read_flags


def _disk_mount_table(path: str) -> Optional[List[Tuple[int, str]]]:
    """Parse guest-absolute mountpoints plus their mount ids, or ``None``.

    Field 5 is decoded for octal escapes (space/tab/newline/backslash); a
    malformed table is unavailable rather than a weaker st_dev-only guess.
    """
    try:
        data = _read_bytes_optional(path, 8 * 1024 * 1024)
    except HelperError:
        return None
    if data is None or not data:
        return None
    entries: List[Tuple[int, str]] = []
    for line in data.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 6:
            return None
        try:
            mount_id = int(fields[0])
        except ValueError:
            return None
        if mount_id < 0:
            return None
        mount = _unescape_mount(fields[4])
        if not mount.startswith("/") or "//" in mount:
            return None
        if len(mount) > 1 and mount.endswith("/"):
            return None
        if _dotdot(mount) or any(ord(ch) < 32 or ord(ch) == 127 for ch in mount):
            return None
        entries.append((mount_id, mount))
    return entries or None


def _disk_wire_path_ok(path: str) -> bool:
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    if path == "/":
        return True
    if len(path) > DISK_USAGE_MAX_WIRE_PATH_CHARS:
        return False
    if path.endswith("/") or "//" in path:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return False
    return not any(part in (".", "..") for part in path.split("/"))


def _disk_unavailable_snapshot(reason: str) -> Dict[str, Any]:
    return {
        "status": "unavailable",
        "root_used_bytes": None,
        "root_device": None,
        "directories": [],
        "visited_entries": 0,
        "excluded_mounts": 0,
        "error_count": 0,
        "reason": _disk_reason(reason),
    }


def _disk_unavailable(reason: str) -> Dict[str, Any]:
    return _disk_unavailable_snapshot(reason)


def _disk_plan_rows(
    root_row: _DiskRow,
    rows: Dict[str, _DiskRow],
    extras: Sequence[str],
    row_limit: int,
) -> Tuple[List[_DiskRow], Set[str], bool]:
    """Reserved measured extras/ancestors first, then top ordinary rows.

    Reserved evidence is at most 129 rows including the root direct row; the
    remainder holds the largest depth<=3 aggregates with their ancestry.
    """
    selected: List[_DiskRow] = [root_row]
    reserved_paths: Set[str] = {root_row.path}
    for extra in extras:
        if extra not in rows:
            continue
        prefix = ""
        for part in [item for item in extra.split("/") if item]:
            prefix += "/" + part
            depth = _disk_path_depth(prefix)
            if depth > DISK_USAGE_MAX_DEPTH and prefix != extra:
                continue
            row = rows.get(prefix)
            if row is not None and row.path not in reserved_paths:
                selected.append(row)
                reserved_paths.add(row.path)
    if len(selected) > DISK_USAGE_MAX_RESERVED_ROWS:
        raise _DiskUnavailable("the reserved disk row budget was exceeded")
    budget = row_limit - len(selected)
    if budget < 0:
        raise _DiskUnavailable("the row budget is smaller than the reserved evidence")
    candidates = [
        row
        for row in rows.values()
        if row.depth <= DISK_USAGE_MAX_DEPTH
        and row.path not in reserved_paths
        and row.allocated > 0
    ]
    candidates.sort(key=lambda row: (-row.allocated, row.path))
    trimmed = False
    for row in candidates:
        if budget <= 0:
            trimmed = True
            break
        if row.path in reserved_paths:
            continue
        missing: List[_DiskRow] = []
        ancestor = ""
        for part in [item for item in row.path.split("/") if item][:-1]:
            ancestor += "/" + part
            parent = rows.get(ancestor)
            if (
                parent is not None
                and parent.path not in reserved_paths
                and parent not in missing
            ):
                missing.append(parent)
        if len(missing) + 1 > budget:
            trimmed = True
            continue
        for parent in missing:
            selected.append(parent)
            reserved_paths.add(parent.path)
            budget -= 1
        selected.append(row)
        reserved_paths.add(row.path)
        budget -= 1
    return selected, reserved_paths, trimmed


def _disk_build_snapshot(
    *,
    status: str,
    root_used: Optional[int],
    root_device: Optional[int],
    root_row: _DiskRow,
    rows: Dict[str, _DiskRow],
    extras: Sequence[str],
    visited: int,
    excluded: int,
    errors: int,
    reason: str,
    row_limit: int,
) -> Tuple[Dict[str, Any], Set[str]]:
    selected, reserved_paths, trimmed = _disk_plan_rows(
        root_row, rows, extras, row_limit
    )
    if trimmed and status == "complete":
        status = "partial"
        reason = reason or "the bounded row limit was reached"
    directories: List[Dict[str, Any]] = []
    for row in selected:
        allocated = row.direct if row is root_row else row.allocated
        directories.append(
            {
                "path": row.path,
                "depth": row.depth,
                "allocated_bytes": int(allocated),
                "direct_bytes": int(row.direct),
            }
        )
    snapshot = {
        "status": status,
        "root_used_bytes": root_used,
        "root_device": root_device,
        "directories": directories,
        "visited_entries": int(visited),
        "excluded_mounts": int(excluded),
        "error_count": int(errors),
        "reason": _disk_reason(reason),
    }
    return snapshot, reserved_paths


def _disk_encode_snapshot(
    snapshot: Dict[str, Any], reserved_paths: Set[str]
) -> bytes:
    """Serialize one snapshot, trimming only ordinary rows to the wire bound."""
    while True:
        try:
            data = json.dumps(
                snapshot, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise _DiskUnavailable(f"the disk snapshot is not serializable: {exc}")
        if len(data) <= DISK_USAGE_MAX_WIRE_BYTES:
            return data + b"\n"
        directories = snapshot.get("directories")
        if not isinstance(directories, list):
            raise _DiskUnavailable("the disk snapshot rows are unavailable")
        ordinary = [
            index
            for index, row in enumerate(directories)
            if isinstance(row, dict) and row.get("path") not in reserved_paths
        ]
        if not ordinary:
            raise _DiskUnavailable(
                "the reserved disk evidence exceeds the bounded wire size"
            )
        drop = max(1, len(ordinary) // 2)
        for index in reversed(ordinary[-drop:]):
            del directories[index]
        snapshot["status"] = "partial"
        snapshot["reason"] = _disk_reason(
            "the result was truncated to the bounded wire size"
        )


def _disk_send(
    write_fd: int, snapshot: Dict[str, Any], reserved_paths: Set[str]
) -> None:
    data = _disk_encode_snapshot(snapshot, reserved_paths)
    view = memoryview(data)
    try:
        while view:
            written = os.write(write_fd, view)
            if written <= 0:
                os._exit(0)
            view = view[written:]
    except OSError:
        os._exit(0)


def _disk_validate_snapshot(
    payload: Dict[str, Any], allowed_extras: Set[str]
) -> Dict[str, Any]:
    keys = {
        "status",
        "root_used_bytes",
        "root_device",
        "directories",
        "visited_entries",
        "excluded_mounts",
        "error_count",
        "reason",
    }
    if set(payload) != keys:
        raise ValueError("unexpected disk snapshot fields")
    status = payload["status"]
    if status not in DISK_USAGE_STATUSES:
        raise ValueError("unsupported disk status")

    def optional_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid optional integer")
        return value

    def count(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid count")
        return value

    root_used = optional_int(payload["root_used_bytes"])
    root_device = optional_int(payload["root_device"])
    visited = count(payload["visited_entries"])
    excluded = count(payload["excluded_mounts"])
    errors = count(payload["error_count"])
    reason = payload["reason"]
    if not isinstance(reason, str):
        raise ValueError("invalid disk reason")
    directories = payload["directories"]
    if not isinstance(directories, list) or len(directories) > DISK_USAGE_MAX_ROWS:
        raise ValueError("invalid disk directory rows")
    rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in directories:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "depth",
            "allocated_bytes",
            "direct_bytes",
        }:
            raise ValueError("invalid disk row")
        path = item["path"]
        if not _disk_wire_path_ok(path) or path in seen:
            raise ValueError("invalid disk path")
        depth = item["depth"]
        if (
            isinstance(depth, bool)
            or not isinstance(depth, int)
            or depth < 0
            or depth > DISK_USAGE_MAX_EXTRA_DEPTH
            or depth != _disk_path_depth(path)
        ):
            raise ValueError("invalid disk depth")
        if depth > DISK_USAGE_MAX_DEPTH and path not in allowed_extras:
            raise ValueError("unsolicited deep disk path")
        allocated = count(item["allocated_bytes"])
        direct = count(item["direct_bytes"])
        if direct > allocated:
            raise ValueError("direct bytes exceed allocated bytes")
        seen.add(path)
        rows.append(
            {
                "path": path,
                "depth": depth,
                "allocated_bytes": allocated,
                "direct_bytes": direct,
            }
        )
    return {
        "status": status,
        "root_used_bytes": root_used,
        "root_device": root_device,
        "directories": rows,
        "visited_entries": visited,
        "excluded_mounts": excluded,
        "error_count": errors,
        "reason": _disk_reason(reason),
    }


def _disk_decode_snapshot(
    line: bytes, allowed_extras: Set[str]
) -> Optional[Dict[str, Any]]:
    if not line or len(line) > DISK_USAGE_MAX_WIRE_BYTES:
        return None
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return _disk_validate_snapshot(payload, allowed_extras)
    except ValueError:
        return None


def _disk_close_frame(frame: _DiskFrame) -> None:
    try:
        frame.entries.close()
    except Exception:  # noqa: BLE001 - close must never mask a measurement
        pass
    try:
        os.close(frame.fd)
    except OSError:
        pass


def _disk_enter_directory(
    frame: _DiskFrame,
    name: str,
    child: str,
    child_depth: int,
    *,
    path_flags: int,
    read_flags: int,
    root_dev: int,
    root_mnt: int,
    extra_set: Set[str],
    max_depth: int,
    rows: Dict[str, _DiskRow],
) -> Tuple[Optional[_DiskFrame], int, int, str]:
    """Open/verify one child directory; never cross a mount or symlink."""
    try:
        path_fd = os.open(name, path_flags, dir_fd=frame.fd)
    except FileNotFoundError:
        return None, 0, 0, ""
    except OSError:
        return None, 0, 1, "some directory metadata could not be read"
    try:
        try:
            path_st = os.fstat(path_fd)
        except OSError:
            return None, 0, 1, "some directory metadata could not be read"
        if not stat.S_ISDIR(path_st.st_mode):
            # Replaced between the directory listing and the safe open.
            return None, 0, 0, ""
        if int(path_st.st_dev) != root_dev:
            return None, 1, 0, ""
        mount_id = _fd_mount_id(path_fd)
        if mount_id is None:
            raise _DiskUnavailable("a directory mount identity is unavailable")
        if mount_id != root_mnt:
            return None, 1, 0, ""
        try:
            read_fd = os.open(f"/proc/self/fd/{path_fd}", read_flags)
        except FileNotFoundError:
            return None, 0, 0, ""
        except OSError:
            return None, 0, 1, "a directory could not be opened for reading"
        try:
            try:
                read_st = os.fstat(read_fd)
            except OSError:
                return None, 0, 1, "some directory metadata could not be read"
            if (
                int(read_st.st_dev) != int(path_st.st_dev)
                or int(read_st.st_ino) != int(path_st.st_ino)
            ):
                return None, 1, 0, ""
            read_mnt = _fd_mount_id(read_fd)
            if read_mnt is None:
                raise _DiskUnavailable("a directory mount identity is unavailable")
            if read_mnt != root_mnt:
                return None, 1, 0, ""
            row: Optional[_DiskRow] = None
            if child_depth <= max_depth or child in extra_set:
                row = rows.get(child)
                if row is None:
                    row = _DiskRow(child, child_depth)
                    rows[child] = row
            active = frame.rows if row is None else frame.rows + (row,)
            try:
                entries = os.scandir(read_fd)
            except OSError:
                return None, 0, 1, "a directory could not be enumerated"
            owned_fd = read_fd
            read_fd = -1
            return (
                _DiskFrame(owned_fd, child, child_depth, entries, active, row),
                0,
                0,
                "",
            )
        finally:
            if read_fd != -1:
                try:
                    os.close(read_fd)
                except OSError:
                    pass
    finally:
        try:
            os.close(path_fd)
        except OSError:
            pass


def _disk_worker_main(
    *,
    spec: Dict[str, Any],
    root: str,
    mountinfo_path: str,
    write_fd: int,
    clock: Callable[[], float],
) -> None:
    flags = _disk_open_flags()
    if flags is None:
        raise _DiskUnavailable(
            "this Python/kernel lacks O_PATH/O_NOATIME/O_NOFOLLOW directory guards"
        )
    path_flags, read_flags = flags
    before = _disk_mount_table(mountinfo_path)
    if before is None:
        raise _DiskUnavailable("the guest mount table is unavailable")
    deadline = clock() + spec["timeout_seconds"]
    extra_set = set(spec["extra_paths"])
    root_row = _DiskRow("/", 0)
    rows: Dict[str, _DiskRow] = {}
    visited = 0
    excluded = 0
    errors = 0
    partial_reason = ""
    seen: Set[Tuple[int, int]] = set()

    try:
        path_fd = os.open(root, path_flags)
    except OSError as exc:
        raise _DiskUnavailable(
            f"cannot open the guest root safely: {exc.strerror or exc}"
        )
    try:
        try:
            path_st = os.fstat(path_fd)
        except OSError as exc:
            raise _DiskUnavailable(
                f"cannot stat the guest root: {exc.strerror or exc}"
            )
        if not stat.S_ISDIR(path_st.st_mode):
            raise _DiskUnavailable("the guest root is not a directory")
        root_dev = int(path_st.st_dev)
        root_ino = int(path_st.st_ino)
        root_mnt = _fd_mount_id(path_fd)
        if root_mnt is None:
            raise _DiskUnavailable("the guest root mount identity is unavailable")
        if not any(
            mount_id == root_mnt and mount == "/" for mount_id, mount in before
        ):
            raise _DiskUnavailable(
                "the guest root mount is not described by the mount table"
            )
        try:
            root_fd = os.open(f"/proc/self/fd/{path_fd}", read_flags)
        except OSError as exc:
            raise _DiskUnavailable(
                "cannot reopen the guest root without following links: "
                f"{exc.strerror or exc}"
            )
    finally:
        try:
            os.close(path_fd)
        except OSError:
            pass

    try:
        try:
            root_st = os.fstat(root_fd)
        except OSError as exc:
            raise _DiskUnavailable(
                f"cannot re-stat the guest root: {exc.strerror or exc}"
            )
        if (
            int(root_st.st_dev) != root_dev
            or int(root_st.st_ino) != root_ino
        ):
            raise _DiskUnavailable("the guest root changed while it was opened")
        read_mnt = _fd_mount_id(root_fd)
        if read_mnt is None:
            raise _DiskUnavailable("the guest root mount identity is unavailable")
        if read_mnt != root_mnt:
            raise _DiskUnavailable("the guest root mount changed while it was opened")
        try:
            root_used: Optional[int] = _fs_used_bytes(root)
        except OSError:
            root_used = None
            errors += 1
            partial_reason = "the root filesystem usage is unavailable"
        mount_paths = {mount for _mount_id, mount in before if mount != "/"}
        try:
            entries = os.scandir(root_fd)
        except OSError as exc:
            raise _DiskUnavailable(
                f"failed to enumerate the guest root: {exc.strerror or exc}"
            )
        stack: List[_DiskFrame] = [
            _DiskFrame(root_fd, "/", 0, entries, (), root_row)
        ]
        root_fd = -1  # owned by the root frame
        last_progress = clock()
        try:
            while stack:
                now = clock()
                if now >= deadline:
                    partial_reason = "the scan time limit was reached"
                    break
                if visited >= spec["max_entries"]:
                    partial_reason = "the scan entry limit was reached"
                    break
                frame = stack[-1]
                try:
                    entry = next(frame.entries)
                except StopIteration:
                    _disk_close_frame(stack.pop())
                    continue
                except OSError:
                    errors += 1
                    partial_reason = "a directory could not be enumerated completely"
                    _disk_close_frame(stack.pop())
                    continue
                visited += 1
                name = entry.name
                if (
                    not isinstance(name, str)
                    or not name
                    or name in (".", "..")
                    or "/" in name
                    or "\x00" in name
                ):
                    errors += 1
                    partial_reason = "a directory entry name was not usable"
                    continue
                child = (
                    "/" + name
                    if frame.path == "/"
                    else frame.path + "/" + name
                )
                if child in mount_paths:
                    excluded += 1
                    continue
                try:
                    entry_st = os.stat(name, dir_fd=frame.fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError:
                    errors += 1
                    partial_reason = "some directory metadata could not be read"
                    continue
                if stat.S_ISDIR(entry_st.st_mode):
                    child_depth = frame.depth + 1
                    if child_depth > DISK_USAGE_PHYSICAL_DEPTH:
                        partial_reason = (
                            "the physical traversal depth limit was reached"
                        )
                        continue
                    new_frame, excluded_inc, error_inc, why = (
                        _disk_enter_directory(
                            frame,
                            name,
                            child,
                            child_depth,
                            path_flags=path_flags,
                            read_flags=read_flags,
                            root_dev=root_dev,
                            root_mnt=root_mnt,
                            extra_set=extra_set,
                            max_depth=spec["max_depth"],
                            rows=rows,
                        )
                    )
                    excluded += excluded_inc
                    errors += error_inc
                    if why:
                        partial_reason = why
                    if new_frame is not None:
                        stack.append(new_frame)
                elif stat.S_ISREG(entry_st.st_mode):
                    if int(entry_st.st_dev) != root_dev:
                        excluded += 1
                        continue
                    # A bind-mounted file can share st_dev with the root while
                    # living on a different mount.  Verify its fd mount id
                    # before accounting, even when the mount table omitted it.
                    try:
                        file_fd = os.open(name, path_flags, dir_fd=frame.fd)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        errors += 1
                        partial_reason = "some file metadata could not be read"
                        continue
                    try:
                        try:
                            file_st = os.fstat(file_fd)
                        except OSError:
                            errors += 1
                            partial_reason = "some file metadata could not be read"
                            continue
                        if not stat.S_ISREG(file_st.st_mode):
                            continue
                        if (
                            int(file_st.st_dev) != int(entry_st.st_dev)
                            or int(file_st.st_ino) != int(entry_st.st_ino)
                        ):
                            # Replaced between the listing and the safe open.
                            continue
                        file_mnt = _fd_mount_id(file_fd)
                        if file_mnt is None:
                            raise _DiskUnavailable(
                                "a file mount identity is unavailable"
                            )
                        if file_mnt != root_mnt:
                            excluded += 1
                            continue
                    finally:
                        try:
                            os.close(file_fd)
                        except OSError:
                            pass
                    identity = (int(file_st.st_dev), int(file_st.st_ino))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    allocated = max(0, int(file_st.st_blocks) * 512)
                    for row in frame.rows:
                        row.allocated += allocated
                    if frame.own_row is not None:
                        frame.own_row.direct += allocated
                now = clock()
                if now - last_progress >= DISK_USAGE_PROGRESS_SECONDS:
                    last_progress = now
                    progress, reserved = _disk_build_snapshot(
                        status="partial",
                        root_used=root_used,
                        root_device=root_dev,
                        root_row=root_row,
                        rows=rows,
                        extras=spec["extra_paths"],
                        visited=visited,
                        excluded=excluded,
                        errors=errors,
                        reason="scan in progress",
                        row_limit=DISK_USAGE_PROGRESS_ROWS,
                    )
                    _disk_send(write_fd, progress, reserved)
        finally:
            while stack:
                _disk_close_frame(stack.pop())
        after = _disk_mount_table(mountinfo_path)
        if after is None:
            raise _DiskUnavailable("the guest mount table became unavailable")
        if after != before:
            partial_reason = partial_reason or "the mount table changed during the scan"
        if errors and not partial_reason:
            partial_reason = "some filesystem metadata could not be read"
        status = "partial" if (partial_reason or errors) else "complete"
        snapshot, reserved = _disk_build_snapshot(
            status=status,
            root_used=root_used,
            root_device=root_dev,
            root_row=root_row,
            rows=rows,
            extras=spec["extra_paths"],
            visited=visited,
            excluded=excluded,
            errors=errors,
            reason=partial_reason,
            row_limit=DISK_USAGE_MAX_ROWS,
        )
        _disk_send(write_fd, snapshot, reserved)
    finally:
        if root_fd != -1:
            try:
                os.close(root_fd)
            except OSError:
                pass


def _disk_worker(
    *,
    spec: Dict[str, Any],
    root: str,
    mountinfo_path: str,
    write_fd: int,
    clock: Callable[[], float],
) -> None:
    """Forked worker: only the bounded progress pipe may be used."""
    for std_fd in (0, 1, 2):
        try:
            os.close(std_fd)
        except OSError:
            pass
    try:
        _disk_worker_main(
            spec=spec,
            root=root,
            mountinfo_path=mountinfo_path,
            write_fd=write_fd,
            clock=clock,
        )
    except _DiskUnavailable as exc:
        _disk_send(write_fd, _disk_unavailable_snapshot(str(exc)), set())
    except BaseException as exc:  # noqa: BLE001 - stdout stays valid JSON
        try:
            _disk_send(
                write_fd,
                _disk_unavailable_snapshot(f"{type(exc).__name__}: {exc}"),
                set(),
            )
        except BaseException:
            pass
    finally:
        os._exit(0)


def _disk_try_reap(pid: int) -> bool:
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return reaped == pid


def _disk_terminate(pid: int) -> bool:
    """SIGTERM then SIGKILL after a bounded grace; never wait indefinitely."""
    if _disk_try_reap(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return _disk_try_reap(pid)
    grace_end = time.monotonic() + DISK_USAGE_KILL_GRACE_SECONDS
    while time.monotonic() < grace_end:
        if _disk_try_reap(pid):
            return True
        time.sleep(0.01)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return _disk_try_reap(pid)
    grace_end = time.monotonic() + DISK_USAGE_KILL_GRACE_SECONDS
    while time.monotonic() < grace_end:
        if _disk_try_reap(pid):
            return True
        time.sleep(0.01)
    return False


def _disk_collect(
    spec: Dict[str, Any],
    *,
    root: str,
    mountinfo_path: str,
    clock: Callable[[], float],
) -> Dict[str, Any]:
    extras = set(spec["extra_paths"])
    try:
        read_fd, write_fd = os.pipe()
    except OSError as exc:
        return _disk_unavailable(
            f"cannot create the disk progress pipe: {exc.strerror or exc}"
        )
    if read_fd <= 2 or write_fd <= 2:
        # The worker closes inherited standard descriptors; a low pipe fd would
        # be part of that set, so refuse rather than lose the only channel.
        try:
            os.close(read_fd)
        except OSError:
            pass
        try:
            os.close(write_fd)
        except OSError:
            pass
        return _disk_unavailable(
            "the helper's standard descriptors are not available for the disk pipe"
        )
    try:
        pid = os.fork()
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        return _disk_unavailable(
            f"cannot fork the disk worker: {exc.strerror or exc}"
        )
    if pid == 0:
        try:
            os.close(read_fd)
            _disk_worker(
                spec=spec,
                root=root,
                mountinfo_path=mountinfo_path,
                write_fd=write_fd,
                clock=clock,
            )
        finally:
            os._exit(0)
    os.close(write_fd)
    latest: Optional[Dict[str, Any]] = None
    buffer = b""
    reaped = False
    eof = False
    timed_out = False
    deadline = (
        time.monotonic()
        + spec["timeout_seconds"]
        + DISK_USAGE_KILL_GRACE_SECONDS
    )

    def consume(chunk: bytes) -> None:
        nonlocal buffer, latest
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            parsed = _disk_decode_snapshot(line, extras)
            if parsed is not None:
                latest = parsed
        if len(buffer) > DISK_USAGE_MAX_WIRE_BYTES:
            buffer = b""

    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            try:
                ready, _writeable, _exceptional = select.select(
                    [read_fd], [], [], min(0.05, deadline - now)
                )
            except (OSError, ValueError):
                break
            if ready:
                try:
                    chunk = os.read(read_fd, DISK_USAGE_READ_CHUNK)
                except BlockingIOError:
                    chunk = None
                except OSError:
                    chunk = b""
                if chunk == b"":
                    eof = True
                    break
                if chunk:
                    consume(chunk)
            elif _disk_try_reap(pid):
                reaped = True
                eof = True
                break
        if not eof:
            drain_end = time.monotonic() + DISK_USAGE_DRAIN_SECONDS
            while time.monotonic() < drain_end:
                try:
                    ready, _writeable, _exceptional = select.select(
                        [read_fd], [], [], 0
                    )
                except (OSError, ValueError):
                    break
                if not ready:
                    break
                try:
                    chunk = os.read(read_fd, DISK_USAGE_READ_CHUNK)
                except (BlockingIOError, OSError):
                    break
                if chunk == b"":
                    break
                consume(chunk)
    finally:
        if not reaped:
            _disk_terminate(pid)
        try:
            os.close(read_fd)
        except OSError:
            pass
    if latest is None:
        return _disk_unavailable(
            "the disk worker did not report a bounded snapshot"
        )
    if (
        latest["status"] == "partial"
        and latest.get("reason") == "scan in progress"
    ):
        latest = dict(latest)
        latest["reason"] = _disk_reason(
            "the disk worker did not finish within the deadline"
            if timed_out
            else "the disk worker ended before a final snapshot"
        )
    return latest


def disk_usage(
    request: Dict[str, Any],
    *,
    sysroot: str = "/",
    mountinfo_path: str = "/proc/self/mountinfo",
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Any]:
    """Bounded, metadata-only root-filesystem directory usage facts.

    Runs the walk in a forked worker so blocked filesystem I/O can never hang
    the RPC parent: progress snapshots arrive over a bounded pipe at most once
    per second, and the parent keeps the latest valid snapshot while enforcing
    the worker deadline.  All descriptor openings/statvfs/walking happen inside
    that worker; directories are opened ``O_PATH|O_NOFOLLOW``, verified against
    the root ``st_dev``/fdinfo ``mnt_id`` and reopened ``O_NOATIME`` so no
    directory access time is refreshed.  A missing guard is ``unavailable``,
    never a weaker st_dev-only walk.  This is read-only diagnostics: it is not
    a deletion, ACL or cleanup entitlement.
    """
    spec = _validate_disk_usage_request(request)
    root = sysroot.rstrip("/") or "/"
    if _disk_open_flags() is None:
        return _disk_unavailable(
            "this Python/kernel lacks O_PATH/O_NOATIME/O_NOFOLLOW; "
            "refusing to refresh directory access times"
        )
    if not hasattr(os, "fork"):
        return _disk_unavailable("this Python does not provide os.fork")
    return _disk_collect(
        spec, root=root, mountinfo_path=mountinfo_path, clock=clock
    )


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #


def _require_capture_id(value: Any) -> str:
    if not isinstance(value, str) or not CAPTURE_ID_RE.match(value):
        raise HelperError(
            "capture_id must be '<64 hex namespace>/<32 hex uuid>'",
            detail={"capture_id": value if isinstance(value, str) else None},
        )
    return value


def _validate_blob_path(blob_path: Any, capture_id: str, spool_root: str) -> str:
    if not isinstance(blob_path, str) or not blob_path.startswith("/") or _dotdot(blob_path):
        raise HelperError("blob_path must be an absolute traversal-free path")
    expected_dir = spool_root.rstrip("/") + "/" + capture_id
    if os.path.dirname(blob_path.rstrip("/")) != expected_dir:
        raise HelperError(
            "blob_path must live inside the capture directory",
            detail={"blob_path": blob_path, "expected_dir": expected_dir},
        )
    if not BLOB_BASENAME_RE.match(os.path.basename(blob_path.rstrip("/"))):
        raise HelperError(
            "blob_path must end with '<64 hex>.blob'", detail={"blob_path": blob_path}
        )
    return blob_path


def _manifest_path(capture_dir: str) -> str:
    return os.path.join(capture_dir, MANIFEST_FILENAME)


def _manifest_digest(files: Sequence[Dict[str, Any]]) -> str:
    canonical = json.dumps(list(files), separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _load_manifest(path: str) -> Optional[Dict[str, Any]]:
    data = _read_bytes_optional(path, MAX_MANIFEST_BYTES)
    if data is None:
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HelperError(
            "capture manifest is not valid JSON; refusing to guess",
            detail={"path": path, "reason": str(exc)},
        )
    if not isinstance(parsed, dict) or not isinstance(parsed.get("files"), list):
        raise HelperError("capture manifest is malformed", detail={"path": path})
    if parsed.get("manifest_sha256") != _manifest_digest(parsed["files"]):
        raise HelperError(
            "capture manifest checksum mismatch; refusing to guess",
            detail={"path": path},
        )
    return parsed


def _save_manifest(
    capture_dir: str,
    capture_id: str,
    created_ns: int,
    files: Sequence[Dict[str, Any]],
) -> None:
    ordered = sorted(files, key=lambda entry: entry["blob_path"])
    payload = {
        "capture_id": capture_id,
        "namespace": capture_id.split("/", 1)[0],
        "created_ns": created_ns,
        "files": ordered,
        "manifest_sha256": _manifest_digest(ordered),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    path = _manifest_path(capture_dir)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(capture_dir)


def _receipts_dir(capture_dir: str) -> str:
    return os.path.join(capture_dir, RECEIPTS_DIRNAME)


def _receipt_path(capture_dir: str, blob_path: str) -> str:
    return os.path.join(_receipts_dir(capture_dir), os.path.basename(blob_path) + ".json")


def _receipt_digest(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "capture_id": payload["capture_id"],
            "entry": payload["entry"],
            "blob": payload["blob"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _blob_meta(st: os.stat_result) -> Dict[str, int]:
    return {
        "device": int(st.st_dev),
        "inode": int(st.st_ino),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }


def _save_receipt(
    capture_dir: str,
    capture_id: str,
    entry: Dict[str, Any],
    blob_st: os.stat_result,
) -> None:
    """Persist one durable per-blob receipt (atomic, fsynced, root-only)."""
    receipts = _receipts_dir(capture_dir)
    _mkdir_root_only(receipts)
    payload = {
        "capture_id": capture_id,
        "entry": entry,
        "blob": _blob_meta(blob_st),
    }
    payload["receipt_sha256"] = _receipt_digest(payload)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    path = _receipt_path(capture_dir, entry["blob_path"])
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise HelperError(
            f"cannot create the capture receipt {tmp}: {exc.strerror or exc}",
            detail={"path": tmp},
        )
    try:
        _write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(receipts)


def _parse_receipt(raw: bytes, *, path: str, capture_id: str, blob_path: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HelperError(
            "capture receipt is not valid JSON; refusing to guess",
            detail={"path": path, "reason": str(exc)},
        )
    if not isinstance(parsed, dict) or not isinstance(parsed.get("entry"), dict):
        raise HelperError("capture receipt is malformed", detail={"path": path})
    if not isinstance(parsed.get("blob"), dict):
        raise HelperError("capture receipt lacks blob provenance", detail={"path": path})
    if parsed.get("capture_id") != capture_id:
        raise HelperError(
            "capture receipt belongs to a different capture", detail={"path": path}
        )
    if parsed["entry"].get("blob_path") != blob_path:
        raise HelperError(
            "capture receipt does not match the requested blob", detail={"path": path}
        )
    if parsed.get("receipt_sha256") != _receipt_digest(parsed):
        raise HelperError(
            "capture receipt checksum mismatch; refusing to guess", detail={"path": path}
        )
    meta = parsed["blob"]
    for key in ("device", "inode", "size", "mtime_ns"):
        value = meta.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HelperError(
                "capture receipt has malformed blob metadata",
                detail={"path": path, "key": key},
            )
    return parsed


def _load_receipt(capture_dir: str, capture_id: str, blob_path: str) -> Optional[Dict[str, Any]]:
    path = _receipt_path(capture_dir, blob_path)
    data = _read_bytes_optional(path, MAX_MANIFEST_BYTES)
    if data is None:
        return None
    return _parse_receipt(data, path=path, capture_id=capture_id, blob_path=blob_path)


def _load_receipts(capture_dir: str, capture_id: str) -> Dict[str, Dict[str, Any]]:
    """Load every durable per-blob receipt (crash recovery before the manifest)."""
    directory = _receipts_dir(capture_dir)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise HelperError(
            f"failed to enumerate {directory}: {exc.strerror or exc}",
            detail={"path": directory},
        )
    receipts: Dict[str, Dict[str, Any]] = {}
    for name in sorted(names):
        if not name.endswith(".json"):
            continue
        base = name[: -len(".json")]
        if not BLOB_BASENAME_RE.match(base):
            continue
        blob_path = os.path.join(capture_dir, base)
        receipt = _load_receipt(capture_dir, capture_id, blob_path)
        if receipt is not None:
            receipts[blob_path] = receipt
    return receipts


def _released_path(capture_dir: str) -> str:
    return os.path.join(capture_dir, RELEASED_FILENAME)


def _load_released(capture_dir: str) -> Dict[str, Dict[str, Any]]:
    """Read the importer-owned cumulative release marker ({} when absent).

    A corrupt marker is a hard error: a released blob must never be recaptured
    and an unknown marker must never authorise skipping one.
    """
    path = _released_path(capture_dir)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise HelperError(
            f"cannot stat the release marker {path}: {exc.strerror or exc}",
            detail={"path": path},
        )
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise HelperError(
            "the release marker is not a root-owned, root-only regular file",
            detail={"path": path},
        )
    data = _read_bytes_optional(path, MAX_MANIFEST_BYTES)
    if data is None:
        return {}
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HelperError(
            "the release marker is not valid JSON; refusing to guess",
            detail={"path": path, "reason": str(exc)},
        )
    if not isinstance(parsed, dict) or not isinstance(parsed.get("released"), dict):
        raise HelperError("the release marker is malformed", detail={"path": path})
    released = parsed["released"]
    canonical = json.dumps(released, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if parsed.get("released_sha256") != hashlib.sha256(canonical).hexdigest():
        raise HelperError(
            "the release marker checksum mismatch; refusing to guess",
            detail={"path": path},
        )
    for base, record in released.items():
        if not isinstance(base, str) or not BLOB_BASENAME_RE.match(base):
            raise HelperError(
                "the release marker names an invalid blob", detail={"path": path}
            )
        if not isinstance(record, dict):
            raise HelperError(
                "the release marker record is malformed", detail={"path": path, "blob": base}
            )
        digest = record.get("sha256")
        released_ns = record.get("released_ns")
        if not isinstance(digest, str) or not digest:
            raise HelperError(
                "the release marker record lacks a digest",
                detail={"path": path, "blob": base},
            )
        if isinstance(released_ns, bool) or not isinstance(released_ns, int):
            raise HelperError(
                "the release marker record lacks a release timestamp",
                detail={"path": path, "blob": base},
            )
    return released


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise HelperError("short write while persisting capture data")
        view = view[written:]

def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _mkdir_root_only(path: str) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise HelperError(
            f"cannot create {path}: {exc.strerror or exc}", detail={"path": path}
        )
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise HelperError(f"{path} is not a directory", detail={"path": path})
    if st.st_uid != os.geteuid():
        raise HelperError(
            f"{path} is not owned by the current user", detail={"path": path}
        )
    if st.st_mode & 0o077:
        raise HelperError(
            f"{path} is not root-only (mode {oct(st.st_mode & 0o777)})", detail={"path": path}
        )


def _verify_spool_chain(spool_root: str, capture_id: str) -> bool:
    root = spool_root.rstrip("/")
    capture_dir = root + "/" + capture_id
    for path in (root, root + "/" + capture_id.split("/", 1)[0], capture_dir):
        try:
            st = os.lstat(path)
        except OSError:
            return False
        if (
            not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_mode & 0o077
        ):
            return False
    receipts = _receipts_dir(capture_dir)
    if os.path.lexists(receipts):
        try:
            st = os.lstat(receipts)
        except OSError:
            return False
        if (
            not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_mode & 0o077
        ):
            return False
    marker = _released_path(capture_dir)
    if os.path.lexists(marker):
        try:
            st = os.lstat(marker)
        except OSError:
            return False
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_mode & 0o077
        ):
            return False
    return True


def _ensure_capture_dirs(spool_root: str, capture_id: str) -> str:
    root = spool_root.rstrip("/")
    namespace = capture_id.split("/", 1)[0]
    capture_dir = root + "/" + capture_id
    _mkdir_root_only(root)
    _mkdir_root_only(root + "/" + namespace)
    _mkdir_root_only(capture_dir)
    return capture_dir


def _copy_prefix(
    src_joined: str,
    blob_path: str,
    expected_dev: int,
    expected_ino: int,
    size: int,
) -> Tuple[str, os.stat_result]:
    """Copy exactly ``size`` bytes, hashing during the copy."""
    try:
        parent_fd, filename = _open_parent(src_joined)
        try:
            src_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise HelperError(
            f"cannot open capture source {src_joined}: {exc.strerror or exc}",
            detail={"path": src_joined},
        )
    tmp = blob_path + ".partial"
    try:
        st = os.fstat(src_fd)
        if not stat.S_ISREG(st.st_mode):
            raise HelperError(
                "capture source is not a regular file", detail={"path": src_joined}
            )
        if (int(st.st_dev), int(st.st_ino)) != (expected_dev, expected_ino):
            raise HelperError(
                "capture source identity changed before copying",
                detail={"path": src_joined},
            )
        if st.st_size < size:
            raise HelperError(
                "capture source prefix is truncated",
                detail={"path": src_joined, "size": int(st.st_size), "wanted": size},
            )
        try:
            out_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise HelperError(
                f"cannot create capture blob {tmp}: {exc.strerror or exc}",
                detail={"path": tmp},
            )
        digest = hashlib.sha256()
        try:
            remaining = size
            while remaining > 0:
                chunk = os.read(src_fd, min(COPY_CHUNK, remaining))
                if not chunk:
                    raise HelperError(
                        "short read while freezing capture prefix",
                        detail={"path": src_joined, "remaining": remaining},
                    )
                digest.update(chunk)
                _write_all(out_fd, chunk)
                remaining -= len(chunk)
            os.fsync(out_fd)
        finally:
            os.close(out_fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, blob_path)
        _fsync_dir(os.path.dirname(blob_path))
        final = os.lstat(blob_path)
        if final.st_size != size:
            raise HelperError(
                "captured blob has an unexpected size", detail={"path": blob_path}
            )
        return digest.hexdigest(), st
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        os.close(src_fd)


def _find_inode_under_roots(
    sysroot: str, wanted: Tuple[int, int], profile: str
) -> Optional[Tuple[str, os.stat_result]]:
    roots: Tuple[str, ...]
    if profile == "npm":
        roots = (NPM_LOG_ROOT,)
    elif profile == "pbs":
        roots = (PBS_TASK_ROOT, PBS_API_ROOT)
    else:
        roots = GUEST_LOG_ROOTS
    seen = 0
    for root in roots:
        joined_root = _join(sysroot, root)
        if not os.path.isdir(joined_root):
            continue
        for dirpath, dirnames, filenames in _walk(joined_root):
            dirnames[:] = [d for d in dirnames if d != QUARANTINE_DIRNAME]
            for name in filenames:
                seen += 1
                if seen > INODE_SEARCH_LIMIT:
                    raise HelperError(
                        "aborting inode search: too many candidate entries",
                        detail={"limit": INODE_SEARCH_LIMIT},
                    )
                joined = os.path.join(dirpath, name)
                try:
                    st = os.lstat(joined)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and (int(st.st_dev), int(st.st_ino)) == wanted:
                    return joined, st
    return None


def _locate_capture_source(
    sysroot: str, spec: Dict[str, Any], index: int
) -> Tuple[str, os.stat_result]:
    wanted = (_int_field(spec, "device", index=index), _int_field(spec, "inode", index=index))
    size = _int_field(spec, "size", index=index)
    requested = _join(sysroot, spec["path"])
    candidate: Optional[Tuple[str, os.stat_result]] = None
    try:
        st = os.lstat(requested)
    except OSError:
        st = None
    if st is not None and stat.S_ISREG(st.st_mode) and (int(st.st_dev), int(st.st_ino)) == wanted:
        candidate = (requested, st)
    if candidate is None:
        candidate = _find_inode_under_roots(sysroot, wanted, spec["profile"])
    if candidate is None:
        raise HelperError(
            "capture source is unreachable by its recorded identity "
            "(renamed, truncated or deleted before capture)",
            detail={"path": spec["path"], "device": wanted[0], "inode": wanted[1]},
        )
    path, st = candidate
    if st.st_size < size:
        raise HelperError(
            "capture source prefix is truncated",
            detail={"path": spec["path"], "size": int(st.st_size), "wanted": size},
        )
    return path, st


def _entry_matches_spec(
    entry: Dict[str, Any], spec: Dict[str, Any], capture_id: str, spool_root: str
) -> bool:
    """Structural match between a recorded entry and the request record."""
    if entry.get("blob_path") != spec.get("blob_path"):
        return False
    for key in ("size", "device", "inode", "compression", "profile", "log_kind"):
        if entry.get(key) != spec.get(key):
            return False
    if not isinstance(entry.get("sha256"), str) or not entry["sha256"]:
        return False
    captured = entry.get("captured_bytes")
    if isinstance(captured, bool) or not isinstance(captured, int) or captured < 0:
        return False
    try:
        _validate_blob_path(entry["blob_path"], capture_id, spool_root)
    except HelperError:
        return False
    return True


def _blob_matches_receipt(entry: Dict[str, Any], receipt: Dict[str, Any]) -> bool:
    """True when the on-disk blob is unchanged since the receipt was written.

    This is deliberately stat-only (device/inode/size/mtime_ns): the original raw
    digest was computed once during the copy.  Re-hashing a multi-GiB blob on
    every resume/fetch is what the receipt journal exists to avoid; full-content
    verification belongs to the manager before it prunes.
    """
    try:
        st = os.lstat(entry["blob_path"])
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        return False
    if int(st.st_size) != int(entry["captured_bytes"]):
        return False
    meta = receipt.get("blob")
    if not isinstance(meta, dict):
        return False
    try:
        expected = (
            int(meta["device"]),
            int(meta["inode"]),
            int(meta["size"]),
            int(meta["mtime_ns"]),
        )
    except (KeyError, TypeError, ValueError):
        return False
    observed = (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))
    return observed == expected


def _release_satisfies(entry: Dict[str, Any], releases: Dict[str, Dict[str, Any]]) -> bool:
    """True when an absent blob was acknowledged and released by the importer."""
    record = releases.get(os.path.basename(entry["blob_path"]))
    if not isinstance(record, dict):
        return False
    return record.get("sha256") == entry.get("sha256")


def capture(
    request: Dict[str, Any],
    *,
    sysroot: str,
    spool_root: str = SPOOL_ROOT,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """Freeze initial high-water prefixes into the node spool (resumable)."""
    _reject_plugin_request(request, operation="capture")
    now = now or time.time_ns()
    capture_id = _require_capture_id(request.get("capture_id"))
    specs = _request_files(
        request,
        require=("blob_path", "size", "device", "inode", "compression", "profile", "log_kind"),
    )
    for index, spec in enumerate(specs):
        _validate_guest_log_path(spec["path"])
        _validate_blob_path(spec["blob_path"], capture_id, spool_root)
        _validate_compression(spec, index=index)
        _validate_profile(spec, index=index)
        _validate_log_kind(spec, index=index)
        _validate_source_provenance(spec, index=index)
        _int_field(spec, "size", index=index)
        _int_field(spec, "device", index=index)
        _int_field(spec, "inode", index=index)

    capture_dir = spool_root.rstrip("/") + "/" + capture_id
    known: Dict[str, Dict[str, Any]] = {}
    loaded_receipts: Dict[str, Dict[str, Any]] = {}
    releases: Dict[str, Dict[str, Any]] = {}
    created_ns = now
    if os.path.lexists(capture_dir):
        manifest = _load_manifest(_manifest_path(capture_dir))
        if not _verify_spool_chain(spool_root, capture_id):
            raise HelperError(
                "the existing capture spool is not a root-owned, root-only "
                "directory tree; refusing to resume",
                detail={"capture_id": capture_id},
            )
        # Durable per-blob receipts are authoritative: they survive a crash
        # between the copy and the (once-per-call) aggregate manifest write.
        loaded_receipts = _load_receipts(capture_dir, capture_id)
        for blob_path, receipt in loaded_receipts.items():
            known[blob_path] = receipt["entry"]
        if manifest is not None:
            created_ns = int(manifest.get("created_ns", now))
            for entry in manifest["files"]:
                if isinstance(entry, dict) and isinstance(entry.get("blob_path"), str):
                    known.setdefault(entry["blob_path"], entry)
        releases = _load_released(capture_dir)

    results: Dict[str, Dict[str, Any]] = {}
    needed = 0
    for spec in specs:
        blob_path = spec["blob_path"]
        entry = known.get(blob_path)
        if entry is None or not _entry_matches_spec(entry, spec, capture_id, spool_root):
            needed += int(spec["size"])
            continue
        known_receipt = loaded_receipts.get(blob_path)
        if known_receipt is not None and _blob_matches_receipt(entry, known_receipt):
            results[blob_path] = entry
        elif not os.path.lexists(blob_path) and _release_satisfies(entry, releases):
            # Acknowledged + released by the importer: never recapture, even if
            # the guest original has rotated away since.
            results[blob_path] = entry
        else:
            needed += int(spec["size"])

    free = _fs_free_bytes(_nearest_existing(spool_root))
    if free < needed + FREE_RESERVE_BYTES:
        raise HelperError(
            "insufficient node spool space for capture",
            detail={
                "free_bytes": free,
                "needed_bytes": needed,
                "reserve_bytes": FREE_RESERVE_BYTES,
            },
        )

    capture_dir = _ensure_capture_dirs(spool_root, capture_id)

    added = False
    try:
        for index, spec in enumerate(specs):
            blob_path = spec["blob_path"]
            if blob_path in results:
                continue
            size = _int_field(spec, "size", index=index)
            src_joined, _st = _locate_capture_source(sysroot, spec, index)
            digest, copied_st = _copy_prefix(
                src_joined,
                blob_path,
                _int_field(spec, "device", index=index),
                _int_field(spec, "inode", index=index),
                size,
            )
            entry = _wire_from_stat(
                copied_st,
                path=spec["path"],
                compression=spec["compression"],
                profile=spec["profile"],
                log_kind=spec["log_kind"],
                is_active=bool(spec.get("is_active", False)),
            )
            entry["size"] = size
            entry["sha256"] = digest
            entry["captured_bytes"] = size
            entry["captured_ns"] = now
            entry["blob_path"] = blob_path
            entry["source_path"] = _guest_rel(sysroot, src_joined)
            # One small fsynced receipt per blob: the aggregate manifest is
            # written once per call instead of after every blob.
            _save_receipt(capture_dir, capture_id, entry, os.lstat(blob_path))
            results[blob_path] = entry
            known[blob_path] = entry
            added = True
    except BaseException:
        # Retain everything already frozen: the aggregate manifest is refreshed
        # once so a later resume (or an operator) still sees the completed blobs.
        if added:
            try:
                _save_manifest(capture_dir, capture_id, created_ns, list(known.values()))
            except Exception:  # noqa: BLE001 - never mask the original failure
                pass
        raise

    _save_manifest(capture_dir, capture_id, created_ns, list(known.values()))
    ordered = [results[spec["blob_path"]] for spec in specs]
    return {"capture_id": capture_id, "files": ordered}


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #


def _snapshot_live_source(
    spec: Dict[str, Any], index: int, sysroot: Optional[str]
) -> Tuple[int, os.stat_result]:
    _validate_guest_log_path(spec["path"])
    offset = _int_field(spec, "offset", index=index)
    length = _int_field(spec, "length", index=index)
    device = _int_field(spec, "device", index=index)
    inode = _int_field(spec, "inode", index=index)
    joined = _join(sysroot, spec["path"])
    try:
        parent_fd, filename = _open_parent(joined)
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise HelperError(
            f"cannot open snapshot source {spec['path']}: {exc.strerror or exc}",
            detail={"path": spec["path"]},
        )
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HelperError(
                "snapshot source is not a regular file", detail={"path": spec["path"]}
            )
        if (int(st.st_dev), int(st.st_ino)) != (device, inode):
            raise HelperError(
                "snapshot source identity changed; re-probe required",
                detail={"path": spec["path"]},
            )
        if st.st_size < spec["size"] or offset + length > spec["size"] or st.st_size < offset + length:
            raise HelperError(
                "snapshot source is truncated below the requested range",
                detail={"path": spec["path"], "size": int(st.st_size)},
            )
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def _snapshot_blob_source(
    spec: Dict[str, Any], index: int, spool_root: str
) -> Tuple[int, os.stat_result, Dict[str, int]]:
    capture_id = _require_capture_id(spec.get("capture_id"))
    blob_path = _validate_blob_path(spec.get("blob_path"), capture_id, spool_root)
    if not _verify_spool_chain(spool_root, capture_id):
        raise HelperError("capture spool has unsafe directory permissions or ownership")
    digest = spec.get("sha256")
    if not isinstance(digest, str) or not digest:
        raise HelperError(
            "captured blob snapshots require the recorded sha256",
            detail={"path": blob_path},
        )
    capture_dir = os.path.dirname(blob_path.rstrip("/"))
    receipt = _load_receipt(capture_dir, capture_id, blob_path)
    if receipt is None:
        raise HelperError(
            "capture receipt is missing; cannot validate the frozen blob",
            detail={"path": blob_path},
        )
    if receipt["entry"].get("sha256") != digest:
        raise HelperError(
            "capture blob digest does not match the recorded provenance",
            detail={"path": blob_path},
        )
    meta = receipt["blob"]
    offset = _int_field(spec, "offset", index=index)
    length = _int_field(spec, "length", index=index)
    try:
        parent_fd, filename = _open_parent(blob_path)
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise HelperError(
            f"cannot open capture blob {blob_path}: {exc.strerror or exc}",
            detail={"path": blob_path},
        )
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HelperError("capture blob is not a regular file", detail={"path": blob_path})
        if st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise HelperError(
                "capture blob is not root-owned and root-only",
                detail={"path": blob_path},
            )
        if st.st_size < offset + length:
            raise HelperError(
                "capture blob is shorter than the requested range",
                detail={"path": blob_path, "size": int(st.st_size)},
            )
        # Stat-only validation against the fsynced receipt: the raw digest was
        # computed once at capture, so a multi-GiB blob is never re-hashed per
        # 32 MiB fetch.  A changed device/inode/size/mtime_ns fails closed.
        observed = (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns))
        expected = (
            int(meta["device"]),
            int(meta["inode"]),
            int(meta["size"]),
            int(meta["mtime_ns"]),
        )
        if observed != expected:
            raise HelperError(
                "capture blob changed since it was captured",
                detail={"path": blob_path},
            )
    except BaseException:
        os.close(fd)
        raise
    return fd, st, meta


def _blob_unchanged(fd: int, meta: Dict[str, int]) -> bool:
    st = os.fstat(fd)
    return (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns)) == (
        int(meta["device"]),
        int(meta["inode"]),
        int(meta["size"]),
        int(meta["mtime_ns"]),
    )


def _read_exact_at(fd: int, offset: int, length: int) -> bytes:
    chunks: List[bytes] = []
    remaining = length
    position = offset
    while remaining > 0:
        chunk = os.pread(fd, min(COPY_CHUNK, remaining), position)
        if not chunk:
            raise HelperError("snapshot source shrank while reading the range")
        chunks.append(chunk)
        position += len(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _tar_add_bytes(
    tar: tarfile.TarFile, name: str, payload: bytes, mode: int, mtime: int
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    info.mtime = mtime
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    tar.addfile(info, io.BytesIO(payload))


def snapshot(
    request: Dict[str, Any],
    *,
    destination: str,
    sysroot: Optional[str] = None,
    spool_root: str = SPOOL_ROOT,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """Write requested ranges into ``destination`` as a tar (<= 32 MiB raw)."""
    _reject_plugin_request(request, operation="snapshot")
    now = now or time.time_ns()
    mtime = int(now // 1_000_000_000)
    specs = _request_files(request, require=("offset", "length"))
    total = 0
    for index, spec in enumerate(specs):
        total += _int_field(spec, "length", index=index)
    if total > MAX_SNAPSHOT_BYTES:
        raise HelperError(
            "snapshot request exceeds the raw byte budget",
            detail={"total_bytes": total, "limit": MAX_SNAPSHOT_BYTES},
        )
    if not isinstance(destination, str) or not destination.startswith("/") or _dotdot(destination):
        raise HelperError("--destination must be an absolute traversal-free path")
    if not os.path.isdir(os.path.dirname(destination)):
        raise HelperError(
            "the snapshot destination directory does not exist",
            detail={"destination": destination},
        )

    opened: List[Tuple[Dict[str, Any], int, os.stat_result, Optional[Dict[str, int]]]] = []
    try:
        for index, spec in enumerate(specs):
            if spec.get("blob_path"):
                fd, st, meta = _snapshot_blob_source(spec, index, spool_root)
            else:
                fd, st = _snapshot_live_source(spec, index, sysroot)
                meta = None
            opened.append((spec, fd, st, meta))

        entries: List[Dict[str, Any]] = []
        for position, (spec, _fd, st, _meta) in enumerate(opened):
            member = f"ranges/{position:06d}.bin"
            compression = spec.get("compression") or _compression_for(
                os.path.basename(spec["path"])
            )
            profile = spec.get("profile") or (
                "npm" if spec["path"].startswith(NPM_LOG_ROOT) else "pbs"
            )
            log_kind = spec.get("log_kind") or "application"
            entry = _wire_from_stat(
                st,
                path=spec["path"],
                compression=compression,
                profile=profile,
                log_kind=log_kind,
                is_active=bool(spec.get("is_active", False)),
            )
            entry.update(
                {
                    "member": member,
                    "offset": _int_field(spec, "offset", index=position),
                    "length": _int_field(spec, "length", index=position),
                    "source": "capture" if spec.get("blob_path") else "live",
                }
            )
            if spec.get("blob_path"):
                entry["blob_path"] = spec["blob_path"]
                entry["capture_id"] = spec["capture_id"]
            entries.append(entry)

        manifest = json.dumps({"files": entries}, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        try:
            with tarfile.open(destination, "w", format=tarfile.USTAR_FORMAT) as tar:
                _tar_add_bytes(tar, "manifest.json", manifest, 0o600, mtime)
                for position, (spec, fd, _st, meta) in enumerate(opened):
                    payload = _read_exact_at(
                        fd,
                        _int_field(spec, "offset", index=position),
                        _int_field(spec, "length", index=position),
                    )
                    if meta is not None and not _blob_unchanged(fd, meta):
                        raise HelperError(
                            "capture blob changed while the range was read",
                            detail={"path": spec["blob_path"]},
                        )
                    _tar_add_bytes(tar, f"ranges/{position:06d}.bin", payload, 0o600, mtime)
        except (OSError, tarfile.TarError) as exc:
            raise HelperError(
                f"failed to write the snapshot tar: {exc}", detail={"destination": destination}
            )
        os.chmod(destination, 0o600)
        return {"files": entries}
    except BaseException:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise
    finally:
        for _spec, fd, _st, _meta in opened:
            try:
                os.close(fd)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Prune
# --------------------------------------------------------------------------- #


def _open_child_dir(fd: int, comp: str, *, create: bool, mode: int) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    for attempt in (0, 1):
        try:
            child = os.open(comp, flags, dir_fd=fd)
        except FileNotFoundError:
            if attempt or not create:
                raise HelperError(
                    f"missing directory component {comp!r}",
                    detail={"component": comp, "errno": errno.ENOENT},
                )
            try:
                os.mkdir(comp, mode, dir_fd=fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise HelperError(
                    f"cannot create directory {comp!r}: {exc.strerror or exc}",
                    detail={"component": comp},
                )
            continue
        except OSError as exc:
            detail = {"component": comp, "errno": exc.errno}
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise HelperError(
                    f"refusing to traverse the symlink/non-directory component {comp!r}",
                    detail=detail,
                )
            raise HelperError(
                f"cannot traverse {comp!r}: {exc.strerror or exc}", detail=detail
            )
        else:
            os.close(fd)
            return child
    raise HelperError("unreachable directory descent state")


def _open_dir_path(joined: str, *, create: bool = False, mode: int = 0o700) -> int:
    """Traverse from a pinned guest root or /; never follow guest components."""
    if not joined.startswith("/"):
        raise HelperError("invalid absolute path", detail={"path": joined})
    parts = [p for p in joined.split("/") if p]
    fd: Optional[int] = None
    for anchor, pinned in _PINNED_GUEST_ROOTS.items():
        if joined == anchor or joined.startswith(anchor + "/"):
            parts = [p for p in joined[len(anchor):].split("/") if p]
            fd = os.dup(pinned)
            break
    if any(part in (".", "..") for part in parts):
        if fd is not None:
            os.close(fd)
        raise HelperError("refusing traversal component", detail={"path": joined})
    if fd is None:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for comp in parts:
            fd = _open_child_dir(fd, comp, create=create, mode=mode)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_parent(joined: str) -> Tuple[int, str]:
    """Open the parent directory of ``joined`` and return (fd, basename)."""
    parent, basename = os.path.split(joined)
    if not basename:
        raise HelperError("invalid absolute file path", detail={"path": joined})
    return _open_dir_path(parent), basename


def _stat_at(dirfd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HelperError(
            f"cannot stat {name}: {exc.strerror or exc}", detail={"name": name}
        )


def _sha256_at(dirfd: int, name: str, length: int) -> Optional[str]:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HelperError(
            f"cannot open {name}: {exc.strerror or exc}", detail={"name": name}
        )
    try:
        digest = hashlib.sha256()
        remaining = length
        while remaining > 0:
            chunk = os.read(fd, min(HASH_CHUNK, remaining))
            if not chunk:
                return None
            digest.update(chunk)
            remaining -= len(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _scan_writable(proc_root: str) -> Set[Tuple[int, int]]:
    """The writable-fd inode set, or a ``blocked`` refusal on unknown state.

    A failed ``/proc`` enumeration means the writer identity cannot be trusted,
    so the caller must never guess -- it becomes a blocking per-file outcome.
    """
    try:
        return _scan_proc(proc_root)[0]
    except HelperError as exc:
        raise _PruneBlocked(str(exc)) from exc


def _used_bytes_or_block(path: str) -> int:
    try:
        return _fs_used_bytes(path)
    except OSError as exc:
        raise _PruneBlocked(
            f"cannot measure free space on {path}: {exc.strerror or exc}"
        ) from exc


def _path_positively_absent(sysroot: str, abs_path: str) -> bool:
    """True only when a no-symlink walk proves the path is missing.

    Missing ancestors count as absence; a symlinked or non-directory component,
    an unreadable component or any other error returns ``False`` so a symlink
    can never masquerade as a deleted file.  Never follows guest symlinks.
    """
    joined = _join(sysroot, abs_path)
    parts = [p for p in joined.split("/") if p]
    fd: Optional[int] = None
    for anchor, pinned in _PINNED_GUEST_ROOTS.items():
        if joined == anchor or joined.startswith(anchor + "/"):
            parts = [p for p in joined[len(anchor):].split("/") if p]
            fd = os.dup(pinned)
            break
    if not parts:
        if fd is not None:
            os.close(fd)
        return False
    if fd is None:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for comp in parts[:-1]:
            try:
                child = os.open(
                    comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            except FileNotFoundError:
                return True
            except OSError:
                return False
            os.close(fd)
            fd = child
        try:
            os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False
    finally:
        os.close(fd)


def _translated_root_owner(sysroot: str) -> Tuple[int, int]:
    """The host-side uid/gid the guest sees as root (from the pinned root).

    An unprivileged container's root directory is owned by its translated host
    ids (e.g. 100000:100000), so quarantine directories created on its behalf
    must carry those ids instead of the node helper's host root 0.
    """
    pinned = _PINNED_GUEST_ROOTS.get(sysroot)
    st = os.fstat(pinned) if pinned is not None else os.stat(sysroot)
    return int(st.st_uid), int(st.st_gid)



def _finalize_quarantine_dir(fd: int, sysroot: str) -> None:
    """Enforce owner-only quarantine permissions under the translated owner.

    An existing directory is acceptable only when owned by the current helper
    or by the translated guest root; anything else is a foreign-owned tree we
    refuse to reuse.  Newly created or previously host-owned directories are
    chowned to the translated guest root so the guest's own root sees them as
    its own.
    """
    uid, gid = _translated_root_owner(sysroot)
    observed = os.fstat(fd)
    owner = observed.st_uid
    if owner not in (os.geteuid(), uid):
        raise _PruneBlocked(
            "quarantine directory has an unexpected owner "
            f"({owner} is neither {os.geteuid()} nor {uid})"
        )
    try:
        if stat.S_IMODE(observed.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        if (observed.st_uid, observed.st_gid) != (uid, gid):
            os.fchown(fd, uid, gid)
        final = os.fstat(fd)
    except OSError as exc:
        raise _PruneBlocked(f"cannot establish quarantine ownership: {exc.strerror or exc}") from exc
    if (final.st_uid, final.st_gid) != (uid, gid) or stat.S_IMODE(final.st_mode) != 0o700:
        raise _PruneBlocked("quarantine ownership or permissions were not observed after repair")


def _prune_protection(path: str, log_kind: Optional[str]) -> Optional[str]:
    name = os.path.basename(path)
    if log_kind == "task_index":
        return "PBS task index/control file"
    if log_kind == "application":
        if NPM_ARCHIVE_RE.match(name):
            return None
        return "current NPM application log"
    if log_kind == "task":
        if name.startswith("UPID:"):
            return None
        return "PBS task control file"
    if log_kind == "api":
        if PBS_API_ARCHIVE_RE.match(name):
            return None
        return "current PBS API log"
    return "unrecognised log kind"


def _validate_quarantine_path(path: Any, log_root: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or _dotdot(path):
        raise HelperError("quarantine_path must be an absolute traversal-free path")
    prefix = log_root + "/" + QUARANTINE_DIRNAME + "/"
    if not path.startswith(prefix):
        raise HelperError(
            "quarantine_path must live under the log root's quarantine directory",
            detail={"quarantine_path": path, "log_root": log_root},
        )
    return path


def _result_entry(spec: Dict[str, Any], state: str, detail: str, reclaimed: int) -> Dict[str, Any]:
    entry = {key: spec[key] for key in WIRE_KEYS if key in spec}
    entry["state"] = state
    entry["detail"] = detail
    entry["bytes_reclaimed"] = int(reclaimed)
    return entry


def _identity_mismatch(spec: Dict[str, Any], st: os.stat_result, index: int) -> Optional[str]:
    if (spec.get("device"), spec.get("inode")) != (int(st.st_dev), int(st.st_ino)):
        return "device/inode changed"
    if spec.get("size") != int(st.st_size):
        return "size changed"
    if spec.get("mtime_ns") != int(st.st_mtime_ns):
        return "mtime changed"
    return None


def _open_quarantine_parent(
    sysroot: str, log_root: str, quarantine: str, *, create: bool = False,
) -> Optional[int]:
    """Pin and validate every private ancestor, translating host-created owners."""
    fd = _open_dir_path(_join(sysroot, log_root))
    components = os.path.dirname(quarantine[len(log_root):]).strip("/").split("/")
    try:
        for component in components:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                raise
            try:
                _finalize_quarantine_dir(child, sysroot)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _quarantine_recheck(
    q_parent_fd: int,
    q_name: str,
    wanted: Tuple[int, int],
    size: int,
    digest: str,
    proc_root: str,
) -> Optional[str]:
    st = _stat_at(q_parent_fd, q_name)
    if st is None:
        return "quarantined file vanished"
    if (int(st.st_dev), int(st.st_ino)) != wanted:
        return "quarantined inode changed"
    if wanted in _scan_writable(proc_root):
        return "an open writable fd appeared during quarantine"
    if _sha256_at(q_parent_fd, q_name, size) != digest:
        return "quarantined digest changed"
    return None


def _restore_or_retain(
    spec: Dict[str, Any],
    parent_fd: int,
    name: str,
    q_parent_fd: int,
    q_name: str,
    reason: str,
) -> Dict[str, Any]:
    if _stat_at(parent_fd, name) is None:
        try:
            os.rename(q_name, name, src_dir_fd=q_parent_fd, dst_dir_fd=parent_fd)
        except OSError as exc:
            return _result_entry(
                spec,
                "conflict",
                f"{reason}; restoring the original failed: {exc.strerror or exc}",
                0,
            )
        return _result_entry(spec, "restored", reason, 0)
    return _result_entry(
        spec,
        "conflict",
        f"{reason}; original path occupied, quarantined file retained",
        0,
    )


def _completed_deletion(spec: Dict[str, Any], root: str, before: int) -> Dict[str, Any]:
    try:
        after = _fs_used_bytes(root)
    except OSError as exc:
        entry = _result_entry(
            spec, "done",
            f"deleted; reclaimed space is unknown ({exc.strerror or exc}); further pruning paused", 0,
        )
        entry["blocked"] = True
        return entry
    return _result_entry(spec, "done", "", max(0, before - after))


def _recover_leftover(
    spec: Dict[str, Any],
    index: int,
    *,
    parent_fd: int,
    name: str,
    q_parent_fd: int,
    q_name: str,
    qst: os.stat_result,
    root_joined: str,
    proc_root: str,
    size: int,
    digest: str,
) -> Dict[str, Any]:
    """Recover an interrupted quarantine recorded by an earlier run."""
    wanted = (int(qst.st_dev), int(qst.st_ino))
    if wanted in _scan_writable(proc_root):
        return _result_entry(
            spec, "conflict", "quarantined file has an open writable fd; retained", 0
        )
    if _stat_at(parent_fd, name) is not None:
        return _result_entry(
            spec, "conflict", "original path is occupied; quarantined file retained", 0
        )
    mismatch = _identity_mismatch(spec, qst, index)
    if mismatch is not None:
        return _result_entry(
            spec, "conflict", f"quarantined file {mismatch}; retained", 0
        )
    observed = _sha256_at(q_parent_fd, q_name, size)
    if observed is None or observed != digest:
        return _result_entry(
            spec, "conflict", "quarantined digest changed; retained", 0
        )
    before = _used_bytes_or_block(root_joined)
    try:
        os.unlink(q_name, dir_fd=q_parent_fd)
        os.fsync(q_parent_fd)
    except OSError as exc:
        raise _PruneBlocked(f"unlink or directory durability failed: {exc.strerror or exc}") from exc
    return _completed_deletion(spec, root_joined, before)


def _prune_one(
    spec: Dict[str, Any],
    index: int,
    *,
    sysroot: str,
    proc_root: str,
) -> Dict[str, Any]:
    path = spec["path"]
    log_root = _validate_guest_log_path(path)
    quarantine = _validate_quarantine_path(spec.get("quarantine_path"), log_root)
    protected = _prune_protection(path, spec.get("log_kind"))
    if protected is not None:
        return _result_entry(spec, "conflict", protected, 0)
    if spec.get("log_kind") == "task":
        try:
            active_tasks = _pbs_active_tasks(sysroot)
        except HelperError as exc:
            raise _PruneBlocked(str(exc)) from exc
        if os.path.basename(path) in active_tasks:
            return _result_entry(spec, "conflict", "currently active PBS task file", 0)
    digest = spec.get("sha256")
    if not isinstance(digest, str) or not digest:
        raise HelperError(
            "prune requests must carry the acknowledged sha256",
            detail={"path": path},
        )
    size = _int_field(spec, "size", index=index)
    wanted = (
        _int_field(spec, "device", index=index),
        _int_field(spec, "inode", index=index),
    )
    _int_field(spec, "mtime_ns", index=index)

    root_joined = _join(sysroot, log_root)
    if not os.path.isdir(root_joined):
        return _result_entry(spec, "conflict", "log root is missing", 0)
    recovery = spec.get("recovery") is True
    if (
        recovery
        and _path_positively_absent(sysroot, path)
        and _path_positively_absent(sysroot, quarantine)
    ):
        return _result_entry(spec, "absent", "both paths absent; deletion origin unknown", 0)

    parent_fd, name = _open_parent(_join(sysroot, path))
    q_parent_fd: Optional[int] = None
    try:
        pst = _stat_at(parent_fd, name)
        q_name = os.path.basename(quarantine)
        q_parent_fd = _open_quarantine_parent(sysroot, log_root, quarantine)
        qst = _stat_at(q_parent_fd, q_name) if q_parent_fd is not None else None

        # A leftover quarantine from an interrupted run is recovered first.
        if (
            qst is not None
            and q_parent_fd is not None
            and (int(qst.st_dev), int(qst.st_ino)) == wanted
        ):
            return _recover_leftover(
                spec,
                index,
                parent_fd=parent_fd,
                name=name,
                q_parent_fd=q_parent_fd,
                q_name=q_name,
                qst=qst,
                root_joined=root_joined,
                proc_root=proc_root,
                size=size,
                digest=digest,
            )
        if pst is None:
            # Both paths observed positively absent is the only recovery outcome
            # that resolves the intent without claiming a fresh deletion: the
            # deletion origin is unknowable.  A fresh candidate stays a conflict.
            if recovery and _path_positively_absent(sysroot, quarantine):
                return _result_entry(
                    spec,
                    "absent",
                    "original and quarantine paths are both absent; "
                    "deletion origin unknown",
                    0,
                )
            return _result_entry(spec, "conflict", "file is missing", 0)
        if not stat.S_ISREG(pst.st_mode):
            raise HelperError(
                "refusing to prune a non-regular file", detail={"path": path}
            )
        try:
            root_dev = int(os.stat(root_joined).st_dev)
        except OSError as exc:
            raise _PruneBlocked(
                f"cannot stat the log filesystem: {exc.strerror or exc}"
            ) from exc
        if int(pst.st_dev) != root_dev:
            return _result_entry(spec, "conflict", "file is outside the log filesystem", 0)
        mismatch = _identity_mismatch(spec, pst, index)
        if mismatch is not None:
            return _result_entry(spec, "conflict", mismatch, 0)
        if wanted in _scan_writable(proc_root):
            return _result_entry(spec, "conflict", "file has an open writable fd", 0)
        observed = _sha256_at(parent_fd, name, size)
        if observed is None:
            return _result_entry(spec, "conflict", "file is missing", 0)
        if observed != digest:
            return _result_entry(spec, "conflict", "digest changed", 0)
        if qst is not None:
            return _result_entry(
                spec, "conflict", "quarantine target holds a different file", 0
            )

        if q_parent_fd is None:
            q_parent_fd = _open_quarantine_parent(sysroot, log_root, quarantine, create=True)
            if q_parent_fd is None:
                raise _PruneBlocked("quarantine parent was not observed after creation")
        before = _used_bytes_or_block(root_joined)
        try:
            os.rename(name, q_name, src_dir_fd=parent_fd, dst_dir_fd=q_parent_fd)
        except OSError as exc:
            return _result_entry(
                spec, "conflict", f"quarantine rename failed: {exc.strerror or exc}", 0
            )
        try:
            reason = _quarantine_recheck(
                q_parent_fd, q_name, wanted, size, digest, proc_root
            )
        except (HelperError, _PruneBlocked, OSError) as exc:
            restored = _restore_or_retain(
                spec, parent_fd, name, q_parent_fd, q_name,
                f"cannot verify the quarantined file ({exc})",
            )
            return _result_entry(spec, "blocked", restored["detail"], 0)
        if reason is None:
            try:
                os.unlink(q_name, dir_fd=q_parent_fd)
                os.fsync(q_parent_fd)
            except OSError as exc:
                restored = _restore_or_retain(
                    spec, parent_fd, name, q_parent_fd, q_name,
                    f"unlink or directory durability failed ({exc.strerror or exc})",
                )
                return _result_entry(spec, "blocked", restored["detail"], 0)
            else:
                return _completed_deletion(spec, root_joined, before)
        return _restore_or_retain(spec, parent_fd, name, q_parent_fd, q_name, reason)
    finally:
        if q_parent_fd is not None:
            os.close(q_parent_fd)
        os.close(parent_fd)


def _validate_recovery(spec: Dict[str, Any], *, index: int) -> None:
    if "recovery" in spec and not isinstance(spec["recovery"], bool):
        raise HelperError(
            f"request files[{index}].recovery must be a boolean",
            detail={"index": index, "path": spec.get("path")},
        )


def prune(
    request: Dict[str, Any],
    *,
    sysroot: str = "/",
    proc_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Identity-checked quarantine deletion of approved closed log files.

    Per-file wire states: ``done`` (deleted), ``restored``/``conflict`` (file
    kept), ``absent`` (only for a ``recovery`` spec whose original *and*
    quarantine paths are both positively missing; resolves without claiming a
    deletion), ``blocked`` (untrusted environment) and ``pending`` (not
    attempted once an earlier file blocked).  A blocked file stops all further
    deletions but preserves every earlier outcome; ``files_pruned`` never counts
    ``absent``.  Structural request faults still raise.
    """
    _reject_plugin_request(request, operation="prune")
    proc = proc_root or _default_proc_root(sysroot)
    specs = _request_files(
        request,
        require=(
            "sha256",
            "quarantine_path",
            "device",
            "inode",
            "size",
            "mtime_ns",
            "compression",
            "profile",
            "log_kind",
        ),
    )
    # Validate every spec before mutating anything so a malformed later entry
    # cannot leave earlier deletions unreported.
    for index, spec in enumerate(specs):
        _validate_compression(spec, index=index)
        _validate_profile(spec, index=index)
        _validate_log_kind(spec, index=index)
        _validate_recovery(spec, index=index)
        digest = spec.get("sha256")
        if not isinstance(digest, str) or not digest:
            raise HelperError(
                "prune requests must carry the acknowledged sha256",
                detail={"index": index, "path": spec["path"]},
            )
        log_root = _validate_guest_log_path(spec["path"])
        _validate_quarantine_path(spec.get("quarantine_path"), log_root)
        for key in ("size", "device", "inode", "mtime_ns"):
            _int_field(spec, key, index=index)

    files: List[Dict[str, Any]] = []
    reclaimed = 0
    pruned = 0
    blocked = False
    for index, spec in enumerate(specs):
        if blocked:
            files.append(
                _result_entry(
                    spec, "pending", "not attempted; an earlier file is blocked", 0
                )
            )
            continue
        try:
            entry = _prune_one(spec, index, sysroot=sysroot, proc_root=proc)
        except (HelperError, _PruneBlocked, OSError) as exc:
            entry = _result_entry(spec, "blocked", str(exc), 0)
            blocked = True
        if entry["state"] == "blocked" or entry.get("blocked") is True:
            blocked = True
        files.append(entry)
        reclaimed += int(entry["bytes_reclaimed"])
        if entry["state"] == "done":
            pruned += 1
    return {"files": files, "bytes_reclaimed": reclaimed, "files_pruned": pruned}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str) + "\n"
    )
    sys.stdout.flush()


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="housekeeping_io")
    parser.add_argument(
        "--operation",
        required=True,
        choices=(
            "probe",
            "disk_usage",
            "logging_acl",
            "capture",
            "snapshot",
            "prune",
        ),
    )
    parser.add_argument("--lxc-id", required=True, dest="lxc_id")
    parser.add_argument("--request")
    parser.add_argument("--request-b64", dest="request_b64")
    parser.add_argument("--destination")
    parser.add_argument("--sysroot")
    parser.add_argument("--mountinfo")
    parser.add_argument("--guest-local", action="store_true", dest="guest_local")
    args = parser.parse_args(argv)
    if not args.request and not args.request_b64:
        parser.error("one of --request or --request-b64 is required")
    if args.request and args.request_b64:
        parser.error("--request and --request-b64 are mutually exclusive")
    if args.operation == "disk_usage":
        if bool(args.sysroot) != bool(args.mountinfo):
            parser.error(
                "--sysroot and --mountinfo must be provided together for disk_usage"
            )
    elif args.mountinfo:
        parser.error("--mountinfo is only valid for --operation disk_usage")
    return args


def _load_request(args: argparse.Namespace) -> Dict[str, Any]:
    if args.request_b64:
        try:
            raw = base64.b64decode(args.request_b64.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise HelperError(f"--request-b64 is not valid base64: {exc}")
    else:
        try:
            with open(args.request, "rb") as handle:
                raw = handle.read(MAX_REQUEST_BYTES + 1)
        except OSError as exc:
            raise HelperError(
                f"cannot read the request file: {exc.strerror or exc}",
                detail={"request": args.request},
            )
        if len(raw) > MAX_REQUEST_BYTES:
            raise HelperError(
                "request file exceeds the size limit", detail={"limit": MAX_REQUEST_BYTES}
            )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HelperError(f"request is not valid JSON: {exc}")
    if not isinstance(parsed, dict):
        raise HelperError("request must be a JSON object")
    return parsed


def _pct_path() -> Optional[str]:
    return _find_binary("/", "pct")


def _run_bounded_process(
    argv: Sequence[str],
    *,
    stdin_bytes: bytes,
    timeout_seconds: float,
    out_limit: int = MAX_REQUEST_BYTES,
    err_limit: int = 64 * 1024,
    pass_fds: Sequence[int] = (),
) -> Tuple[int, bytes, bytes]:
    """Run *argv* with a strict wall deadline and bounded nonblocking teardown.

    ``subprocess.run(timeout=...)`` is not a strict budget for the disk
    operation because its implicit kill/wait can block behind filesystem I/O.
    This helper instead polls both pipes, never blocks on a descriptor, and
    escalates SIGTERM->SIGKILL after a bounded grace without an unbounded wait.
    """
    try:
        stdin_file = tempfile.TemporaryFile()
    except OSError as exc:
        raise HelperError(
            f"failed to prepare stdin for {argv[0]}: {exc.strerror or exc}"
        )
    try:
        try:
            stdin_file.write(stdin_bytes)
            stdin_file.seek(0)
        except OSError as exc:
            raise HelperError(
                f"failed to stage stdin for {argv[0]}: {exc.strerror or exc}"
            )
        try:
            proc = subprocess.Popen(
                list(argv),
                stdin=stdin_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=tuple(pass_fds),
            )
        except OSError as exc:
            raise HelperError(f"failed to execute {argv[0]}: {exc}")
        assert proc.stdout is not None and proc.stderr is not None
        out_fd = proc.stdout.fileno()
        err_fd = proc.stderr.fileno()
        buffers: Dict[int, bytearray] = {out_fd: bytearray(), err_fd: bytearray()}
        limits = {out_fd: out_limit, err_fd: err_limit}
        open_fds: Set[int] = set(buffers)
        for fd in open_fds:
            try:
                os.set_blocking(fd, False)
            except OSError:
                pass
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        overflow = False
        try:
            while open_fds:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    ready, _writeable, _exceptional = select.select(
                        list(open_fds), [], [], min(0.05, remaining)
                    )
                except (OSError, ValueError):
                    break
                for fd in ready:
                    target = buffers.get(fd)
                    if target is None or fd not in open_fds:
                        continue
                    try:
                        chunk = os.read(fd, DISK_USAGE_READ_CHUNK)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if chunk == b"":
                        open_fds.discard(fd)
                        continue
                    target.extend(chunk)
                    if len(target) > limits[fd]:
                        overflow = True
                        break
                if overflow:
                    break
            if not timed_out and not overflow:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    proc.wait(timeout=min(remaining, DISK_USAGE_KILL_GRACE_SECONDS))
                except subprocess.TimeoutExpired:
                    timed_out = True
        finally:
            if proc.poll() is None:
                _terminate_process(proc)
        out = bytes(buffers[out_fd])
        err = bytes(buffers[err_fd])
        if overflow:
            raise HelperError(
                f"{argv[0]} output exceeded the bounded transport limit",
                detail={"limit": max(out_limit, err_limit)},
            )
        if timed_out:
            raise HelperError(
                f"{argv[0]} exceeded its bounded execution deadline",
                detail={
                    "timeout": timeout_seconds,
                    "stdout": out.decode("utf-8", "replace").strip()[-200:],
                    "stderr": err.decode("utf-8", "replace").strip()[-200:],
                },
            )
        rc = proc.returncode
        if rc is None:
            raise HelperError(f"{argv[0]} did not report an exit status")
        return int(rc), out, err
    finally:
        try:
            stdin_file.close()
        except OSError:
            pass


def _terminate_process(proc: "subprocess.Popen[bytes]") -> None:
    """Bounded SIGTERM then SIGKILL; never an unbounded wait."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=DISK_USAGE_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=DISK_USAGE_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # A kernel-blocked process may remain until its I/O returns; never
            # wait for it, and suppress the Popen destructor warning.
            proc.returncode = -signal.SIGKILL
    finally:
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001 - teardown must not mask failure
                pass


def _run_cmd(
    argv: Sequence[str], *, timeout_seconds: Optional[float] = None
) -> Tuple[int, str, str]:
    if timeout_seconds is not None:
        rc, out, err = _run_bounded_process(
            list(argv),
            stdin_bytes=b"",
            timeout_seconds=timeout_seconds,
            out_limit=1024 * 1024,
            err_limit=64 * 1024,
        )
        return (
            rc,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )
    try:
        proc = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=EXEC_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HelperError(f"failed to execute {argv[0]}: {exc}")
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _pct_guest_state(
    lxc_id: str,
    *,
    pct: Optional[str] = None,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    pct = pct or _pct_path()
    if pct is None:
        raise HelperError("pct is not available on this host")

    def state_timeout() -> Optional[float]:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HelperError(
                "the housekeeping node budget is exhausted",
                detail={"lxc_id": lxc_id},
            )
        return max(0.05, min(DISK_USAGE_STATE_TIMEOUT_SECONDS, remaining))

    rc, out, err = _run_cmd(
        [pct, "status", lxc_id], timeout_seconds=state_timeout()
    )
    if rc != 0:
        raise HelperError(
            "pct status failed for the guest",
            detail={"lxc_id": lxc_id, "rc": rc, "stderr": err.strip()[-400:]},
        )
    running = "running" in out.lower()
    rc, out, err = _run_cmd(
        [pct, "config", lxc_id], timeout_seconds=state_timeout()
    )
    if rc != 0:
        raise HelperError(
            "pct config failed for the guest",
            detail={"lxc_id": lxc_id, "rc": rc, "stderr": err.strip()[-400:]},
        )
    values: Dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    return {
        "name": values.get("hostname") or lxc_id,
        "os_type": values.get("ostype") or "",
        "is_running": running,
        "is_template": values.get("template", "0") in ("1", "true", "yes"),
    }


def _pct_guest_call(
    pct: str,
    operation: str,
    lxc_id: str,
    request: Dict[str, Any],
    *,
    timeout_seconds: Optional[float] = None,
    out_limit: Optional[int] = None,
) -> Dict[str, Any]:
    payload = json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(payload) > MAX_REQUEST_BYTES:
        raise HelperError("guest request exceeds the bounded request limit")
    encoded = base64.b64encode(payload).decode("ascii")
    argv = [
        pct,
        "exec",
        lxc_id,
        "--",
        "python3",
        "-c",
        (
            "import sys; request=sys.stdin.buffer.readline(); source=sys.stdin.buffer.read(); "
            "sys.argv.extend(['--request-b64',request.decode('ascii').strip()]); "
            "exec(compile(source,'<fleet-housekeeping>','exec'),"
            "{'__name__':'__main__','__file__':'<fleet-housekeeping>'})"
        ),
        "--operation",
        operation,
        "--lxc-id",
        lxc_id,
        "--guest-local",
    ]
    source_path = os.path.abspath(__file__)
    try:
        with open(source_path, "rb") as source:
            source_bytes = source.read()
    except OSError as exc:
        raise HelperError(f"cannot re-read the helper source: {exc}")
    try:
        rc, out_bytes, err_bytes = _run_bounded_process(
            argv,
            stdin_bytes=encoded.encode("ascii") + b"\n" + source_bytes,
            timeout_seconds=(
                EXEC_TIMEOUT * 10 if timeout_seconds is None else timeout_seconds
            ),
            out_limit=MAX_REQUEST_BYTES if out_limit is None else out_limit,
            err_limit=64 * 1024,
        )
    except HelperError as exc:
        raise HelperError(f"failed to run the in-container helper: {exc}")
    out = out_bytes.decode("utf-8", "replace")
    err = err_bytes.decode("utf-8", "replace")
    if rc != 0:
        raise HelperError(
            "the in-container housekeeping helper failed",
            detail={
                "rc": rc,
                "stdout": out.strip()[-400:],
                "stderr": err.strip()[-400:],
            },
        )
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        raise HelperError(
            "the in-container helper returned invalid JSON",
            detail={"reason": str(exc), "stdout": out.strip()[-400:]},
        )
    if not isinstance(parsed, dict):
        raise HelperError("the in-container helper returned a non-object payload")
    if "error" in parsed:
        raise HelperError(str(parsed.get("error")), detail=parsed.get("detail") or {})
    return parsed


def _resolve_sysroot(args: argparse.Namespace) -> Optional[str]:
    if args.sysroot:
        root = args.sysroot.rstrip("/") or "/"
        if not os.path.isdir(root):
            raise HelperError("--sysroot is not a directory", detail={"sysroot": root})
        return root
    if args.guest_local:
        return "/"
    if _pct_path() is not None and args.lxc_id:
        command = ["lxc-info", "-n", str(args.lxc_id), "-p", "-H"]
        observed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        pid = observed.stdout.strip()
        if observed.returncode != 0 or not pid.isdigit() or int(pid) <= 0:
            raise HelperError("cannot resolve the running guest's init process")
        fd = os.open("/proc/{0}/root".format(pid), os.O_RDONLY | os.O_DIRECTORY)
        try:
            confirmed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
            if confirmed.returncode != 0 or confirmed.stdout.strip() != pid:
                raise HelperError("guest init changed while pinning its root")
        except BaseException:
            os.close(fd)
            raise
        anchor = "/proc/self/fd/{0}".format(fd)
        _PINNED_GUEST_ROOTS[anchor] = fd
        return anchor
    return None


def _required_sysroot(args: argparse.Namespace) -> str:
    root = _resolve_sysroot(args)
    if root is None:
        raise HelperError(
            "a guest sysroot is required; pass --sysroot or run this on the Proxmox node",
            detail={"lxc_id": args.lxc_id},
        )
    return root


def _dispatch_logging_acl(
    args: argparse.Namespace, request: Dict[str, Any]
) -> Dict[str, Any]:
    """Run the descriptor-anchored ACL repair in the guest, never on the node.

    A fixture ``--sysroot``/``--guest-local`` invocation runs in-process; a real
    node call requires ``pct`` and an already-running, non-template guest.  A
    stopped guest is reported as a failed facts object and is never started or
    repaired through a node-side fallback.
    """
    if args.guest_local or args.sysroot:
        return logging_acl(request, sysroot=_resolve_sysroot(args) or "/")
    pct = _pct_path()
    if pct is None:
        return _acl_fail(_acl_result(), "pct is not available on this host")
    try:
        guest = _pct_guest_state(args.lxc_id)
    except HelperError as exc:
        return _acl_fail(
            _acl_result(), "the guest state is unavailable: {0}".format(exc)
        )
    if not guest["is_running"] or guest["is_template"]:
        return _acl_fail(_acl_result(), "the guest is not running")
    return _pct_guest_call(pct, "logging_acl", args.lxc_id, request)


def _dispatch_disk_usage(
    args: argparse.Namespace, request: Dict[str, Any]
) -> Dict[str, Any]:
    """Read-only disk facts: node orchestration or an explicit fixture root.

    Missing pct/Python/a running guest yields an ``unavailable`` snapshot; it
    never downgrades to a node-root walk and never starts the container.
    """
    if args.guest_local or args.sysroot:
        if bool(args.sysroot) != bool(args.mountinfo):
            raise HelperError(
                "disk_usage requires --sysroot and --mountinfo together",
                detail={"operation": "disk_usage"},
            )
        if args.sysroot:
            return disk_usage(
                request,
                sysroot=_resolve_sysroot(args) or "/",
                mountinfo_path=args.mountinfo,
            )
        return disk_usage(
            request,
            sysroot="/",
            mountinfo_path="/proc/self/mountinfo",
        )
    spec = _validate_disk_usage_request(request)
    deadline = time.monotonic() + DISK_USAGE_NODE_BUDGET_SECONDS
    pct = _pct_path()
    if pct is None:
        return _disk_unavailable("pct is not available on this host")
    try:
        guest = _pct_guest_state(args.lxc_id, pct=pct, deadline=deadline)
    except HelperError as exc:
        return _disk_unavailable(f"the guest state is unavailable: {exc}")
    if not guest["is_running"]:
        return _disk_unavailable("the guest is not running")
    remaining = deadline - time.monotonic() - DISK_USAGE_TEARDOWN_SECONDS
    if remaining < DISK_USAGE_MIN_WORKER_SECONDS:
        return _disk_unavailable(
            "the node budget is exhausted before the guest disk call"
        )
    worker_budget = min(
        spec["timeout_seconds"], remaining - DISK_USAGE_TRANSPORT_RESERVE_SECONDS
    )
    if worker_budget < DISK_USAGE_MIN_WORKER_SECONDS:
        return _disk_unavailable(
            "the node budget is too small for the guest disk worker"
        )
    forwarded = dict(request)
    forwarded["timeout_seconds"] = round(worker_budget, 3)
    try:
        payload = _pct_guest_call(
            pct,
            "disk_usage",
            args.lxc_id,
            forwarded,
            timeout_seconds=remaining,
            out_limit=2 * 1024 * 1024,
        )
    except HelperError as exc:
        return _disk_unavailable(f"the guest disk worker is unavailable: {exc}")
    try:
        return _disk_validate_snapshot(payload, set(spec["extra_paths"]))
    except ValueError:
        return _disk_unavailable(
            "the guest disk worker returned malformed disk facts"
        )


def dispatch(args: argparse.Namespace, request: Dict[str, Any]) -> Dict[str, Any]:
    operation = args.operation
    if operation == "logging_acl":
        return _dispatch_logging_acl(args, request)
    if operation == "disk_usage":
        return _dispatch_disk_usage(args, request)
    if operation == "probe":
        if args.guest_local or args.sysroot:
            return probe(request, sysroot=_resolve_sysroot(args) or "/")
        pct = _pct_path()
        if pct is None:
            return probe(request, sysroot="/")
        guest = _pct_guest_state(args.lxc_id)
        if not guest["is_running"]:
            return _stopped_probe_facts(guest)
        forwarded = dict(request)
        forwarded["guest"] = guest
        return _pct_guest_call(pct, "probe", args.lxc_id, forwarded)
    if operation == "prune":
        if args.guest_local or args.sysroot:
            return prune(request, sysroot=_resolve_sysroot(args) or "/")
        pct = _pct_path()
        if pct is None:
            raise HelperError(
                "prune requires a Proxmox node (pct) or an explicit --sysroot/--guest-local"
            )
        guest = _pct_guest_state(args.lxc_id)
        if not guest["is_running"]:
            raise HelperError(
                "refusing to prune: the guest is not running",
                detail={"lxc_id": args.lxc_id},
            )
        # Run against the pinned running init root on the node: the host's
        # initial user namespace can inspect every guest process descriptor,
        # which the container's own root cannot always do.  Never pct exec.
        return prune(request, sysroot=_required_sysroot(args))
    if operation == "capture":
        return capture(request, sysroot=_required_sysroot(args))
    if operation == "snapshot":
        if not args.destination:
            raise HelperError("--destination is required for snapshot")
        return snapshot(
            request,
            destination=args.destination,
            sysroot=(
                None
                if all(isinstance(item, dict) and "blob_path" in item for item in request.get("files", []))
                else _resolve_sysroot(args)
            ),
        )
    raise HelperError(f"unsupported operation {operation!r}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:  # argparse already reported usage
        return int(exc.code or 0)
    try:
        request = _load_request(args)
        facts = dispatch(args, request)
    except HelperError as exc:
        _emit({"error": str(exc), "detail": exc.detail})
        return 1
    except Exception as exc:  # noqa: BLE001 - stdout stays valid JSON
        _emit({"error": f"{type(exc).__name__}: {exc}", "detail": {}})
        return 1
    finally:
        for fd in _PINNED_GUEST_ROOTS.values():
            os.close(fd)
        _PINNED_GUEST_ROOTS.clear()
    _emit(facts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
