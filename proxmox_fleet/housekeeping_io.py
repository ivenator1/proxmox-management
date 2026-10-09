#!/usr/bin/env python3
"""Transport helper for the fleet log housekeeping feature (step 2).

This module is a **standalone stdlib-only** program.  The manager stages it on a
Proxmox node and invokes it as::

    python3 housekeeping_io.py --operation probe    --lxc-id 123 --request req.json
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

Execution topology
------------------
``capture`` / ``snapshot`` run on the node; guest ``path`` values are resolved
against a pinned running init process's private mount root (or explicit test
``--sysroot``); never guess the node's potentially empty rootfs mount. Node
``blob_path`` and ``--destination`` are node-absolute and used verbatim.
``probe`` / ``prune`` need the guest's own ``/proc`` (writer identity), so on a
node the helper obtains guest state via read-only ``pct config``/``pct status``
(never starting the container) and runs its own logic inside the container with
``pct exec <id> -- python3 -`` and the staged helper file on stdin.  Passing
``--guest-local`` (internal) means "do not orchestrate, run against
``--sysroot``" and is what the in-container invocation uses.

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
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

__all__ = [
    "HelperError",
    "probe",
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
)

#: Dependency roots whose symlinks reveal an in-use cache.
DEPENDENCY_ROOTS = ("/app/node_modules",)


class HelperError(RuntimeError):
    """A deterministic transport failure (non-zero exit, JSON on stdout)."""

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.detail: Dict[str, Any] = dict(detail or {})


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
            raise HelperError(
                f"cannot enumerate {fd_dir}: {exc.strerror or exc}; run as root",
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


def _cache_is_dependency(sysroot: str, cache_real: str) -> bool:
    prefixes = {cache_real.rstrip("/"), cache_real.rstrip("/") + "/"}
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
        for sub in ("", ".bin", ".pnpm"):
            directory = os.path.join(root, sub) if sub else root
            for name in _listdir(directory) or []:
                joined = os.path.join(directory, name)
                try:
                    if not os.path.islink(joined):
                        continue
                except OSError:
                    continue
                target = os.path.realpath(joined)
                if target.rstrip("/") in prefixes or target.startswith(cache_real.rstrip("/") + "/"):
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
        if candidate is not None:
            joined = _join(sysroot, candidate)
            resolved = os.path.realpath(joined)
            entry["resolved"] = resolved
            entry["escaped"] = resolved != joined
            entry["exists"] = os.path.isdir(resolved)
            entry["allowlisted"] = not entry["escaped"] and _cache_allowed(candidate, resolved, spec, sysroot)
            entry["root_owned"] = entry["exists"] and os.stat(resolved).st_uid == 0
            if entry["exists"] and entry["allowlisted"]:
                allocated, error = _tree_allocated_lenient(resolved)
                entry["allocated_bytes"] = allocated
                entry["error"] = error
                try:
                    entry["dependency_referenced"] = _cache_is_dependency(sysroot, resolved)
                except HelperError as exc:
                    entry["dependency_referenced"] = True
                    entry["error"] = entry["error"] or str(exc)
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
    }


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
                    f"missing directory component {comp!r}", detail={"component": comp}
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


def _open_dir_optional(joined: str) -> Optional[int]:
    try:
        return _open_dir_path(joined)
    except HelperError:
        return None


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


def _ensure_quarantine_root(sysroot: str, log_root: str) -> None:
    joined = _join(sysroot, log_root + "/" + QUARANTINE_DIRNAME)
    fd = _open_dir_path(joined, create=True)
    try:
        os.fchmod(fd, 0o700)
    except OSError as exc:
        raise HelperError(
            f"cannot enforce quarantine directory permissions: {exc.strerror or exc}",
            detail={"path": joined},
        )
    finally:
        os.close(fd)


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
    if wanted in _scan_proc(proc_root)[0]:
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
    if wanted in _scan_proc(proc_root)[0]:
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
    before = _fs_used_bytes(root_joined)
    try:
        os.unlink(q_name, dir_fd=q_parent_fd)
        os.fsync(q_parent_fd)
    except OSError as exc:
        return _result_entry(
            spec, "conflict", f"unlink failed: {exc.strerror or exc}", 0
        )
    after = _fs_used_bytes(root_joined)
    return _result_entry(spec, "done", "", max(0, before - after))


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
    if spec.get("log_kind") == "task" and os.path.basename(path) in _pbs_active_tasks(sysroot):
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
    root_dev = int(os.stat(root_joined).st_dev)

    parent_fd, name = _open_parent(_join(sysroot, path))
    q_parent_fd: Optional[int] = None
    try:
        pst = _stat_at(parent_fd, name)
        q_name = os.path.basename(quarantine)
        q_parent_fd = _open_dir_optional(_join(sysroot, os.path.dirname(quarantine)))
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
            return _result_entry(spec, "conflict", "file is missing", 0)
        if not stat.S_ISREG(pst.st_mode):
            raise HelperError(
                "refusing to prune a non-regular file", detail={"path": path}
            )
        if int(pst.st_dev) != root_dev:
            return _result_entry(spec, "conflict", "file is outside the log filesystem", 0)
        mismatch = _identity_mismatch(spec, pst, index)
        if mismatch is not None:
            return _result_entry(spec, "conflict", mismatch, 0)
        if wanted in _scan_proc(proc_root)[0]:
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

        _ensure_quarantine_root(sysroot, log_root)
        if q_parent_fd is None:
            # The quarantine path preserves the source-relative directory, so a
            # nested fanout needs its descendants created (root-only) beneath the
            # quarantine root before the file can be renamed into place.  The
            # pinned directory-fd/O_NOFOLLOW helper refuses symlinked or
            # non-directory components rather than following them.
            joined_parent = _join(sysroot, os.path.dirname(quarantine))
            try:
                q_parent_fd = _open_dir_path(joined_parent, create=True, mode=0o700)
            except HelperError as exc:
                raise HelperError(
                    "cannot open the quarantine directory",
                    detail={"quarantine_path": quarantine, "reason": str(exc)},
                ) from exc
            try:
                os.fchmod(q_parent_fd, 0o700)
            except OSError as exc:
                os.close(q_parent_fd)
                q_parent_fd = None
                raise HelperError(
                    f"cannot enforce quarantine directory permissions: {exc.strerror or exc}",
                    detail={"quarantine_path": quarantine},
                ) from exc
        before = _fs_used_bytes(root_joined)
        try:
            os.rename(name, q_name, src_dir_fd=parent_fd, dst_dir_fd=q_parent_fd)
        except OSError as exc:
            return _result_entry(
                spec, "conflict", f"quarantine rename failed: {exc.strerror or exc}", 0
            )
        reason = _quarantine_recheck(q_parent_fd, q_name, wanted, size, digest, proc_root)
        if reason is None:
            try:
                os.unlink(q_name, dir_fd=q_parent_fd)
                os.fsync(q_parent_fd)
            except OSError as exc:
                reason = f"unlink failed: {exc.strerror or exc}"
            else:
                after = _fs_used_bytes(root_joined)
                return _result_entry(spec, "done", "", max(0, before - after))
        return _restore_or_retain(spec, parent_fd, name, q_parent_fd, q_name, reason)
    finally:
        if q_parent_fd is not None:
            os.close(q_parent_fd)
        os.close(parent_fd)


def prune(
    request: Dict[str, Any],
    *,
    sysroot: str = "/",
    proc_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Identity-checked quarantine deletion of approved closed log files."""
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
    files: List[Dict[str, Any]] = []
    reclaimed = 0
    pruned = 0
    for index, spec in enumerate(specs):
        _validate_compression(spec, index=index)
        _validate_profile(spec, index=index)
        _validate_log_kind(spec, index=index)
        entry = _prune_one(spec, index, sysroot=sysroot, proc_root=proc)
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
        choices=("probe", "capture", "snapshot", "prune"),
    )
    parser.add_argument("--lxc-id", required=True, dest="lxc_id")
    parser.add_argument("--request")
    parser.add_argument("--request-b64", dest="request_b64")
    parser.add_argument("--destination")
    parser.add_argument("--sysroot")
    parser.add_argument("--guest-local", action="store_true", dest="guest_local")
    args = parser.parse_args(argv)
    if not args.request and not args.request_b64:
        parser.error("one of --request or --request-b64 is required")
    if args.request and args.request_b64:
        parser.error("--request and --request-b64 are mutually exclusive")
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


def _run_cmd(argv: Sequence[str]) -> Tuple[int, str, str]:
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


def _pct_guest_state(lxc_id: str) -> Dict[str, Any]:
    pct = _pct_path()
    if pct is None:
        raise HelperError("pct is not available on this host")
    rc, out, err = _run_cmd([pct, "status", lxc_id])
    if rc != 0:
        raise HelperError(
            "pct status failed for the guest",
            detail={"lxc_id": lxc_id, "rc": rc, "stderr": err.strip()[-400:]},
        )
    running = "running" in out.lower()
    rc, out, err = _run_cmd([pct, "config", lxc_id])
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
    pct: str, operation: str, lxc_id: str, request: Dict[str, Any]
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
        source = open(source_path, "rb")
    except OSError as exc:
        raise HelperError(f"cannot re-read the helper source: {exc}")
    try:
        proc = subprocess.run(
            argv,
            input=encoded.encode("ascii") + b"\n" + source.read(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=EXEC_TIMEOUT * 10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HelperError(f"failed to run the in-container helper: {exc}")
    finally:
        source.close()
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise HelperError(
            "the in-container housekeeping helper failed",
            detail={
                "rc": proc.returncode,
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


def dispatch(args: argparse.Namespace, request: Dict[str, Any]) -> Dict[str, Any]:
    operation = args.operation
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
        return _pct_guest_call(pct, "prune", args.lxc_id, request)
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
