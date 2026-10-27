"""Behavioural regressions for the housekeeping policy orchestration.

Every test drives :func:`proxmox_fleet.housekeeping.run_housekeeping` (and the
logging-only :func:`prepare_lxc_logging`) through:

* a real :class:`CheckpointStore` SQLite database under a temporary history
  directory (the durable cadence/manifest state),
* a real local HTTP server that accepts bounded Loki drops (204), serves
  readiness and answers the retention marker queries,
* a scripted guest transport whose *facts* are derived from real temporary
  cache directories and log files and which performs the requested native
  actions (deleting cache files, freezing/serving log prefixes) exactly as the
  node helper would.

The assertions are about observable, durable transitions (files reclaimed,
checkpoint rows, returned metrics/status) -- never about forwarding a command
string or a mocked result producer.
"""
from __future__ import annotations

import copy
import glob as glob_module
import hashlib
import http.server
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from proxmox_fleet import housekeeping
from proxmox_fleet import housekeeping_cache as hc
from proxmox_fleet import housekeeping_import as hi
from proxmox_fleet import housekeeping_io as hio
from proxmox_fleet import log_plugins as lp
from proxmox_fleet.alloy import DesiredAlloyConfig
from proxmox_fleet.housekeeping_checkpoint import CheckpointStore, GuestKey
from proxmox_fleet.housekeeping_io import REQUIRED_BINARIES
from proxmox_fleet.models.log_plugins import GuestLogOverride
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult

CLUSTER = "alpha"
NODE = "pve"
LXC = "120"
NPM_ARCHIVE = "/data/logs/access.log.1"

_CATALOGUE = None


def _catalogue():
    """Load the real packaged manifests once per test session."""
    global _CATALOGUE
    if _CATALOGUE is None:
        _CATALOGUE = lp.load_log_catalogue()
    return _CATALOGUE


def _script_identity(guest: "Guest") -> Optional[str]:
    """Explicit identity first, then the fixture's profile shorthand."""
    if guest.script_id:
        return guest.script_id
    if "npm" in guest.profiles:
        return "nginxproxymanager"
    if "pbs" in guest.profiles:
        return "proxmox-backup-server"
    return None


_TOKEN_RE = re.compile(r'\|= "([^"]+)"')
_FILENAME_RE = re.compile(r'filename="([^"]+)"')


# --------------------------------------------------------------------------- #
# Loki stub (real local HTTP: push / ready / query)
# --------------------------------------------------------------------------- #


class _Handler(http.server.BaseHTTPRequestHandler):
    def _send(
        self,
        status: int,
        body: bytes = b"",
        content_type: Optional[str] = None,
        extra: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        stub: "LokiStub" = self.server.stub  # type: ignore[attr-defined]
        if parsed.path == "/ready":
            self._send(stub.ready_status, b"ok")
            return
        if parsed.path == "/loki/api/v1/query_range":
            params = urllib.parse.parse_qs(parsed.query)
            query = params.get("query", [""])[0]
            stub.queries.append(query)
            payload = json.dumps(stub.query_response(query)).encode("utf-8")
            self._send(200, payload, "application/json")
            return
        self._send(404)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        stub: "LokiStub" = self.server.stub  # type: ignore[attr-defined]
        stub.pushes.append((self.path, body, dict(self.headers)))
        if stub.push_status == 204:
            self._send(204)
        else:
            # Retry-After: 0 keeps an injected outage from sleeping in the test.
            self._send(stub.push_status, extra={"Retry-After": "0"})

    def log_message(self, *args: Any) -> None:  # noqa: D401 - silence
        pass


class LokiStub:
    def __init__(self, *, push_status: int = 204, ready_status: int = 200) -> None:
        self.push_status = push_status
        self.ready_status = ready_status
        self.pushes: List[Tuple[str, bytes, Dict[str, str]]] = []
        self.queries: List[str] = []
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.stub = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def query_response(self, query: str) -> Dict[str, Any]:
        token_match = _TOKEN_RE.search(query)
        token = token_match.group(1) if token_match else ""
        filename_match = _FILENAME_RE.search(query)
        if filename_match:
            stream = {"job": "lxc-file", "delivery": "live", "filename": filename_match.group(1)}
        else:
            stream = {"job": "systemd-journal", "role": "guest"}
        return {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [{"stream": stream, "values": [["1", token]]}],
            },
        }

    def __enter__(self) -> "LokiStub":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# --------------------------------------------------------------------------- #
# Real temporary cache directories
# --------------------------------------------------------------------------- #


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


def _cache_dir(tmp_path: Path, name: str, *, files: Sequence[str] = ("pkg.bin",), size: int = 8192) -> Path:
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    for fname in files:
        (directory / fname).write_bytes(b"x" * size)
    return directory


def _tool_for_command(command: str) -> Optional[str]:
    for tool, native in hc.CACHE_CLEAN_COMMANDS.items():
        if command == native:
            return tool
    if command.startswith("LC_ALL=C BUN_INSTALL_CACHE_DIR=") and " pm cache prune --max-age 30" in command:
        return "bun"
    return None


def _bun_reclaimable(root: Optional[Path]) -> int:
    """Mirror of the helper's conservative Bun reclaimable total."""
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


@dataclass
class CacheSpec:
    resolved: str
    directory: Optional[Path] = None
    version: Optional[str] = "1.0.0"
    source: str = "tool"
    rc: int = 0


_ABSENT_CACHE: Dict[str, CacheSpec] = {
    "apt": CacheSpec("/var/cache/apt/archives", None, None, "allowlist"),
    "yarn": CacheSpec("/usr/local/share/.cache/yarn/v6", None, "1.0.0", "tool"),
    "npm": CacheSpec("/root/.npm", None, "1.0.0", "tool"),
    "pnpm": CacheSpec("/root/.local/share/pnpm/store/v11", None, "1.0.0", "tool"),
    "bun": CacheSpec("/root/.bun/install/cache", None, "1.4.4", "tool"),
}


# --------------------------------------------------------------------------- #
# Scripted guest transport
# --------------------------------------------------------------------------- #


def _wire(
    root: Path,
    path: str,
    *,
    compression: str = "plain",
    profile: str = "npm",
    log_kind: str = "application",
    is_active: bool = False,
) -> Dict[str, Any]:
    st = os.stat(root / path.lstrip("/"))
    return {
        "path": path,
        "device": int(st.st_dev),
        "inode": int(st.st_ino),
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "allocated_bytes": int(st.st_blocks) * 512,
        "compression": compression,
        "profile": profile,
        "log_kind": log_kind,
        "is_active": is_active,
    }


class Guest:
    """Scripted executor backed by the real helper on temporary paths."""

    host = "pve"

    def __init__(self, guest_root: Path, spool: Path) -> None:
        self.guest_root = str(guest_root)
        self.spool = str(spool)
        self.name = "guest"
        self.os_type = "debian"
        self.running = True
        self.template = False
        self.profiles: List[str] = []
        self.files: List[Dict[str, Any]] = []
        self.busy_tools: List[str] = []
        self.journal_bytes = 0
        self.access_ready = True
        self.cache_specs: Dict[str, CacheSpec] = dict(_ABSENT_CACHE)
        self.binaries: Dict[str, Any] = {name: "/usr/bin/" + name for name in REQUIRED_BINARIES}
        self.alloy_binary = True
        self.alloy_journal = True
        self.alloy_enabled = True
        self.alloy_active = True
        self.alloy_config_sha = ""
        self.alloy_reconcile_failed = False
        self.journald_hash = ""
        self.alloy_env_hash = ""
        self.npm_logrotate_hash = ""
        self.facts_override: Optional[Dict[str, Any]] = None
        self.probe_script: List[Dict[str, Any]] = []
        self.script_id: Optional[str] = None
        self.log_override: Optional[GuestLogOverride] = None
        self.logging_facts: Optional[Dict[str, Any]] = None
        self.logging_unit_status: Dict[str, str] = {}
        self.logging_source_status: Dict[str, str] = {}
        self.acl_traverse: set = set()
        self.acl_roots: set = set()
        self.acl_files: set = set()
        self.log_acl_calls: List[Dict[str, Any]] = []
        self.log_acl_result: Optional[PrimitiveResult] = None
        self.fail_probe_calls: set = set()
        self.validate_only_fails = False
        self.validation_calls: List[str] = []
        # observability
        self.probe_calls = 0
        self.logging_requests: List[Optional[Dict[str, Any]]] = []
        self.applied: List[str] = []
        self.capture_calls = 0
        self.snapshot_calls = 0
        self.prune_calls: List[List[Dict[str, Any]]] = []
        self.release_calls = 0
        self.alloy_reconciles: List[Dict[str, Any]] = []

    # -- facts ------------------------------------------------------------- #

    def _cache_facts(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for tool, spec in self.cache_specs.items():
            directory = spec.directory
            exists = directory is not None and directory.exists()
            entry = {
                "root": spec.resolved,
                "resolved": spec.resolved,
                "source": spec.source,
                "exists": exists,
                "allowlisted": True,
                "escaped": False,
                "allocated_bytes": _measure(directory) if exists else 0,
                "version": spec.version,
                "dependency_referenced": False,
                "error": None,
                "root_owned": True,
            }
            if tool == "bun":
                entry["binary"] = "/usr/local/bin/bun"
                entry["cache_prune_supported"] = True
                entry["reclaimable_bytes"] = (
                    _bun_reclaimable(directory) if exists else 0
                )
            out[tool] = entry
        return out

    def _evidence(self) -> Dict[str, Any]:
        return {
            "npm": {
                "npm.service": "npm" in self.profiles,
                "openresty.service": "npm" in self.profiles,
                "app_root": "npm" in self.profiles,
                "log_root": "npm" in self.profiles,
                "detected": "npm" in self.profiles,
            },
            "pbs": {
                "proxmox-backup-manager": "pbs" in self.profiles,
                "proxmox-backup-proxy.service": "pbs" in self.profiles,
                "task_root": "pbs" in self.profiles,
                "api_root": "pbs" in self.profiles,
                "detected": "pbs" in self.profiles,
            },
        }

    def _prepared_root(self, root: str) -> bool:
        return bool(self.access_ready or root in self.acl_roots)

    def _unit_status(self, unit: Dict[str, Any]) -> str:
        key = unit["key"]
        if key in self.logging_unit_status:
            return self.logging_unit_status[key]
        if "*" in str(unit.get("name", "")):
            return "inactive"
        return "active"

    def _unit_records(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        records: Dict[str, Any] = {}

        def add(unit: Dict[str, Any]) -> None:
            status = self._unit_status(unit)
            records[unit["key"]] = {"status": status, "instances": {}}

        for layout in entry.get("layouts", []):
            for unit in (*layout.get("required_units", []), *layout.get("running_units_any", [])):
                add(unit)
        for unit in entry.get("journal_units", []):
            records.setdefault(unit["key"], {"status": self._unit_status(unit), "instances": {}})
        return records

    def _root_record(self, root: str) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "exists": False,
            "is_dir": False,
            "is_symlink": False,
            "device": None,
            "blocked_ancestor": False,
            "status": "missing_root",
        }
        joined = Path(self.guest_root) / root.lstrip("/")
        try:
            st = os.lstat(joined)
        except FileNotFoundError:
            return record
        except OSError:
            record.update(exists=True, status="failed")
            return record
        record["exists"] = True
        record["is_symlink"] = stat.S_ISLNK(st.st_mode)
        record["device"] = int(st.st_dev)
        if record["is_symlink"]:
            record["status"] = "failed"
            return record
        if not stat.S_ISDIR(st.st_mode):
            record["status"] = "failed"
            return record
        record["is_dir"] = True
        prepared = self._prepared_root(root)
        record["status"] = "ok" if prepared else "denied"
        record["access"] = {
            "traversable": prepared,
            "readable": prepared,
            "default_acl": prepared,
        }
        return record

    def _exact_record(self, path: str) -> Dict[str, Any]:
        parent = path.rsplit("/", 1)[0] or "/"
        parent_ok = self._prepared_root(parent) or parent in self.acl_traverse
        record: Dict[str, Any] = {
            "exists": False,
            "is_symlink": False,
            "device": None,
            "inode": None,
            "size": None,
            "mtime_ns": None,
            "same_device": True,
            "parent_traversable": bool(parent_ok),
            "status": "quiet" if parent_ok else "missing_root",
        }
        joined = Path(self.guest_root) / path.lstrip("/")
        try:
            st = os.lstat(joined)
        except FileNotFoundError:
            # A safe, existing parent proves the path can be granted traverse
            # access even though the declared file is not there yet; only a
            # missing parent/symlink is ``missing_root``.  Do not conflate the
            # two: an absent optional sibling must not block repair.
            try:
                parent_stat = os.lstat(joined.parent)
            except OSError:
                return record
            if not stat.S_ISDIR(parent_stat.st_mode):
                return record
            record["parent_traversable"] = bool(parent_ok)
            record["status"] = "quiet" if parent_ok else "denied"
            return record
        except OSError:
            record.update(exists=True, status="failed")
            return record
        record.update(
            exists=True,
            is_symlink=stat.S_ISLNK(st.st_mode),
            device=int(st.st_dev),
            inode=int(st.st_ino),
            size=int(st.st_size),
            mtime_ns=int(st.st_mtime_ns),
            is_file=stat.S_ISREG(st.st_mode),
        )
        if record["is_symlink"] or not record["is_file"]:
            record["status"] = "failed"
            return record
        readable = bool(self.access_ready or path in self.acl_files)
        record["access"] = {"readable": readable}
        if readable and parent_ok:
            record["status"] = "ok"
        else:
            record["status"] = "denied"
        return record

    def _source_facts(
        self, descriptor: Dict[str, Any], key: Optional[str] = None
    ) -> Dict[str, Any]:
        forced = self.logging_source_status.get(key) if key is not None else None
        roots = list(descriptor.get("roots", []))
        root_records = {root: self._root_record(root) for root in roots}
        matches: List[Dict[str, Any]] = []
        for pattern in descriptor.get("paths", []):
            candidates = [
                root
                for root in roots
                if pattern == root or pattern.startswith(root + "/")
            ]
            root = max(candidates, key=len) if candidates else None
            status = root_records[root]["status"] if root else "missing_root"
            files: List[Dict[str, Any]] = []
            if status == "ok":
                for match in glob_module.glob(
                    self.guest_root + pattern, recursive=True
                ):
                    path = Path(match)
                    if not path.is_file() or path.is_symlink():
                        continue
                    guest_path = "/" + str(path.relative_to(self.guest_root))
                    if f"/{hio.QUARANTINE_DIRNAME}/" in guest_path:
                        continue
                    try:
                        st = os.lstat(match)
                    except OSError:
                        continue
                    files.append(
                        {
                            "path": guest_path,
                            "device": int(st.st_dev),
                            "inode": int(st.st_ino),
                            "size": int(st.st_size),
                            "mtime_ns": int(st.st_mtime_ns),
                            "accessible": True,
                        }
                    )
                status = "ok" if files else "quiet"
            matches.append(
                {
                    "pattern": pattern,
                    "matched": len(files),
                    "skipped": 0,
                    "truncated": False,
                    "status": status,
                    "files": files,
                }
            )
        exact_records = {
            path: self._exact_record(path)
            for path in descriptor.get("exact_files", [])
        }
        statuses = [record["status"] for record in root_records.values()]
        statuses += [match["status"] for match in matches]
        statuses += [record["status"] for record in exact_records.values()]
        if forced == "unavailable":
            for record in root_records.values():
                record["status"] = "unavailable"
            for match in matches:
                match["status"] = "unavailable"
            for record in exact_records.values():
                record["status"] = "unavailable"
            statuses = ["unavailable"]
        elif forced == "partial":
            for record in root_records.values():
                record["status"] = "partial"
            for match in matches:
                match["status"] = "partial"
                match["truncated"] = True
            for record in exact_records.values():
                record["status"] = "partial"
            statuses = ["partial"]
        elif forced in {"denied", "failed"}:
            for record in root_records.values():
                record["status"] = forced
            for match in matches:
                match["status"] = forced
            for record in exact_records.values():
                record["status"] = forced
            statuses = [forced]
        if "failed" in statuses:
            overall = "failed"
        elif "unavailable" in statuses:
            overall = "unavailable"
        elif "partial" in statuses:
            overall = "partial"
        elif "denied" in statuses:
            overall = "denied"
        elif any(
            record.get("exists") for record in root_records.values()
        ) or any(record.get("exists") for record in exact_records.values()):
            overall = "ok"
        elif statuses and all(item == "quiet" for item in statuses):
            overall = "quiet"
        else:
            overall = "missing_root"
        return {
            "status": overall,
            "truncated": False,
            "roots": root_records,
            "exact_files": exact_records,
            "matches": matches,
        }

    def _logging_facts(
        self, request: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        if request is None:
            return {"plugins": {}, "sources": {}}
        if self.logging_facts is not None:
            return copy.deepcopy(self.logging_facts)
        if not self.running or self.template:
            return {"plugins": {}, "sources": {}}
        plugins: Dict[str, Any] = {}
        for owner, entry in request.get("plugins", {}).items():
            includes: Dict[str, Any] = {}
            for dependency, dependency_entry in entry.get("includes", {}).items():
                includes[dependency] = {
                    "units": self._unit_records(dependency_entry)
                }
            plugins[owner] = {
                "units": self._unit_records(entry),
                "includes": includes,
            }
        sources: Dict[str, Any] = {}
        for key, source_entry in request.get("sources", {}).items():
            sources[key] = {
                "layouts": {
                    layout_id: self._source_facts(descriptor, key)
                    for layout_id, descriptor in source_entry.get(
                        "layouts", {}
                    ).items()
                }
            }
        return {"plugins": plugins, "sources": sources}

    def facts(self, logging_request: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        binaries = dict(self.binaries)
        def policy_hash(name: str, fallback: str) -> str:
            path = Path(self.guest_root) / hio.POLICY_FILES[name].lstrip("/")
            return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else fallback

        if not self.alloy_binary:
            binaries["alloy"] = None
        return {
            "guest": {
                "name": self.name,
                "os_type": self.os_type,
                "is_running": self.running,
                "is_template": self.template,
            },
            "disk": {"total_bytes": 1000, "available_bytes": 500, "used_percent": 50.0},
            "journal_bytes": self.journal_bytes,
            "profiles": list(self.profiles),
            "files": [dict(wire) for wire in self.files],
            "cache_paths": self._cache_facts(),
            "busy_tools": list(self.busy_tools),
            "binaries": binaries,
            "log_access_ready": self.access_ready,
            "log_access_detail": {"alloy_user": True, "dirs": {}, "files": {}},
            "policy_sha256": {
                "journald": policy_hash("journald", self.journald_hash),
                "alloy_env": policy_hash("alloy_env", self.alloy_env_hash),
                "npm_logrotate": policy_hash("npm_logrotate", self.npm_logrotate_hash),
            },
            "profile_evidence": self._evidence(),
            "logging": self._logging_facts(logging_request),
        }

    # -- transport --------------------------------------------------------- #

    def housekeeping_probe(
        self,
        lxc_id: str,
        *,
        logging_request: Optional[Dict[str, Any]] = None,
    ) -> PrimitiveResult:
        self.probe_calls += 1
        self.logging_requests.append(logging_request)
        if self.probe_calls in self.fail_probe_calls:
            return PrimitiveResult(rc=1, failed=True, stderr="probe failed")
        if self.probe_script:
            facts = self.probe_script.pop(0)
        elif self.facts_override is not None:
            facts = self.facts_override
        else:
            facts = self.facts(logging_request)
        return PrimitiveResult(rc=0, facts=facts)

    def housekeeping_apply(self, lxc_id: str, *, command: str) -> PrimitiveResult:
        self.applied.append(command)
        tool = _tool_for_command(command)
        if tool is not None and tool in self.cache_specs:
            spec = self.cache_specs[tool]
            if spec.rc == 0:
                directory = spec.directory
                if directory is not None:
                    links = directory / "links"
                    for path in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                        if not path.is_file() or path.is_symlink():
                            continue
                        if tool == "bun" and (
                            path == links or links in path.parents
                        ):
                            continue
                        path.unlink()
            return PrimitiveResult(
                rc=spec.rc,
                changed=spec.rc == 0,
                failed=spec.rc != 0,
                stderr=("native failure" if spec.rc != 0 else ""),
                facts={},
            )
        from proxmox_fleet import housekeeping_native as native
        import sys

        parts = shlex.split(command)
        if parts[0] == "install":
            (Path(self.guest_root) / parts[-1].lstrip("/")).mkdir(parents=True, exist_ok=True)
        elif parts[:2] == ["python3", "-c"] and parts[2] in (native._WRITE_PROGRAM, native._READ_PROGRAM):
            path = str(Path(self.guest_root) / parts[3].lstrip("/"))
            process = subprocess.run([sys.executable, "-c", parts[2], path, *parts[4:]],
                                     capture_output=True, text=True, check=False)
            return PrimitiveResult(rc=process.returncode, failed=process.returncode != 0,
                                   stdout=process.stdout, stderr=process.stderr, facts={})
        return PrimitiveResult(rc=0, changed=True, facts={})

    def housekeeping_log_acl(
        self,
        lxc_id: str,
        *,
        roots: Sequence[str] = (),
        exact_files: Sequence[str] = (),
    ) -> PrimitiveResult:
        roots = tuple(roots)
        exact_files = tuple(exact_files)
        self.log_acl_calls.append(
            {"roots": roots, "exact_files": exact_files}
        )
        if self.log_acl_result is not None:
            return self.log_acl_result

        def grant_ancestors(path: str) -> None:
            current = path.rsplit("/", 1)[0]
            while current and current != "/":
                self.acl_traverse.add(current)
                current = current.rsplit("/", 1)[0]

        repaired_roots = 0
        repaired_files = 0
        skipped_missing = 0
        changed = False
        for root in roots:
            grant_ancestors(root)
            joined = Path(self.guest_root) / root.lstrip("/")
            try:
                observed = os.lstat(joined)
            except FileNotFoundError:
                skipped_missing += 1
                continue
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
                return PrimitiveResult(
                    rc=0,
                    changed=changed,
                    facts={
                        "status": "failed",
                        "changed": changed,
                        "repaired_roots": repaired_roots,
                        "repaired_files": repaired_files,
                        "skipped_missing": skipped_missing,
                        "error_count": 1,
                        "reason": "declared log root is unsafe",
                    },
                )
            self.acl_roots.add(root)
            repaired_roots += 1
            changed = True
        for path in exact_files:
            grant_ancestors(path)
            joined = Path(self.guest_root) / path.lstrip("/")
            try:
                observed = os.lstat(joined)
            except FileNotFoundError:
                skipped_missing += 1
                continue
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
                return PrimitiveResult(
                    rc=0,
                    changed=changed,
                    facts={
                        "status": "failed",
                        "changed": changed,
                        "repaired_roots": repaired_roots,
                        "repaired_files": repaired_files,
                        "skipped_missing": skipped_missing,
                        "error_count": 1,
                        "reason": "declared exact file is unsafe",
                    },
                )
            self.acl_files.add(path)
            repaired_files += 1
            changed = True
        return PrimitiveResult(
            rc=0,
            changed=changed,
            facts={
                "status": "complete",
                "changed": changed,
                "repaired_roots": repaired_roots,
                "repaired_files": repaired_files,
                "skipped_missing": skipped_missing,
                "error_count": 0,
                "reason": "",
            },
        )

    def housekeeping_capture(self, lxc_id: str, *, files: Sequence[Dict[str, Any]], capture_id: str) -> PrimitiveResult:
        self.capture_calls += 1
        try:
            facts = hio.capture(
                {"capture_id": capture_id, "files": [dict(entry) for entry in files]},
                sysroot=self.guest_root,
                spool_root=self.spool,
            )
        except hio.HelperError as exc:
            return PrimitiveResult(rc=1, failed=True, stderr=str(exc), facts={})
        return PrimitiveResult(rc=0, changed=True, facts=facts)

    def housekeeping_snapshot(self, lxc_id: str, *, files: Sequence[Dict[str, Any]], destination: str) -> PrimitiveResult:
        self.snapshot_calls += 1
        try:
            facts = hio.snapshot(
                {"files": [dict(entry) for entry in files]},
                destination=destination,
                sysroot=self.guest_root,
                spool_root=self.spool,
            )
        except hio.HelperError as exc:
            return PrimitiveResult(rc=1, failed=True, stderr=str(exc), facts={})
        return PrimitiveResult(rc=0, changed=True, facts=facts)

    def housekeeping_prune(self, lxc_id: str, *, files: Sequence[Dict[str, Any]]) -> PrimitiveResult:
        self.prune_calls.append([dict(entry) for entry in files])
        return PrimitiveResult(rc=1, failed=True, stderr="prune must not be reached", facts={})

    def run_shell(self, command: str, **opts: Any) -> PrimitiveResult:
        self.release_calls += 1
        proc = subprocess.run(command, shell=True, capture_output=True, text=True)
        return PrimitiveResult(
            rc=proc.returncode,
            changed=proc.returncode == 0,
            failed=proc.returncode != 0,
            stdout=proc.stdout,
            stderr=proc.stderr,
            facts={},
        )

    # -- alloy ------------------------------------------------------------- #

    def alloy_probe(self, *, lxc_id: Optional[str] = None) -> PrimitiveResult:
        return PrimitiveResult(
            rc=0,
            facts={
                "binary_present": self.alloy_binary,
                "package_manager": "apt",
                "config_sha256": self.alloy_config_sha,
                "journal_member": self.alloy_journal,
                "service_enabled": self.alloy_enabled,
                "service_active": self.alloy_active,
            },
        )

    def alloy_reconcile(
        self,
        *,
        lxc_id: Optional[str] = None,
        desired_content: str,
        install: bool,
        configure: bool,
        add_journal_group: bool,
        repair_service: bool,
        validate_only: bool = False,
    ) -> PrimitiveResult:
        self.alloy_reconciles.append(
            {
                "install": install,
                "configure": configure,
                "add_journal_group": add_journal_group,
                "repair_service": repair_service,
                "validate_only": validate_only,
            }
        )
        if validate_only:
            self.validation_calls.append(desired_content)
            if self.validate_only_fails:
                return PrimitiveResult(rc=1, failed=True, stderr="invalid HCL", facts={})
            return PrimitiveResult(rc=0, changed=False, facts={})
        if self.alloy_reconcile_failed:
            return PrimitiveResult(rc=1, failed=True, stderr="alloy deploy failed", facts={})
        if install:
            self.alloy_binary = True
            # The install-only primitive provisions the guest 'acl' package in
            # the same step, so subsequent probes can measure file access.
            for tool in ("setfacl", "getfacl"):
                if not self.binaries.get(tool):
                    self.binaries[tool] = "/usr/bin/" + tool
            for key, value in list(self.logging_source_status.items()):
                if value == "unavailable":
                    del self.logging_source_status[key]
        if configure:
            self.alloy_config_sha = hashlib.sha256(
                desired_content.encode("utf-8")
            ).hexdigest()
        return PrimitiveResult(rc=0, changed=True, facts={})

    # -- helpers ----------------------------------------------------------- #

    def cache_commands(self) -> List[str]:
        return [
            command
            for command in self.applied
            if command in hc.CACHE_CLEAN_COMMANDS.values()
            or command.startswith("LC_ALL=C BUN_INSTALL_CACHE_DIR=")
        ]


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #


@pytest.fixture
def history(tmp_path: Path) -> Path:
    directory = tmp_path / "history"
    directory.mkdir()
    return directory


@pytest.fixture
def guest(tmp_path: Path, monkeypatch) -> Guest:
    root = tmp_path / "guest"
    (root / "data" / "logs").mkdir(parents=True)
    spool = tmp_path / "spool"
    monkeypatch.setattr(hi, "SPOOL_ROOT", str(spool))
    return Guest(root, spool)


def _base(url: str, *, env: bool = True) -> DesiredAlloyConfig:
    content = 'loki.write "default" {\n  endpoint { url = "%s/loki/api/v1/push" }\n}\n' % url
    content += 'loki.source.journal "journal" {\n'
    content += (
        '  max_age = coalesce(sys.env("FLEET_JOURNAL_MAX_AGE"), "48h")\n'
        if env
        else '  max_age = "12h"\n'
    )
    content += "}\n"
    return DesiredAlloyConfig(content=content, sha256=hashlib.sha256(content.encode()).hexdigest())


def _journald_hash(settings: GlobalSettings) -> str:
    from proxmox_fleet import housekeeping_native as native

    return hashlib.sha256(native.journald_policy_content(settings).encode()).hexdigest()


def _settings(history: Path, *, url: str = "", **over: Any) -> GlobalSettings:
    values: Dict[str, Any] = {
        "fleet_history_dir": str(history),
        "housekeeping_enabled": bool(url),
        "housekeeping_loki_url": url,
    }
    values.update(over)
    return GlobalSettings(**values)


def _run(
    guest: Guest,
    settings: GlobalSettings,
    base: Optional[DesiredAlloyConfig],
    *,
    cluster: str = CLUSTER,
    node: str = NODE,
    lxc_id: str = LXC,
    dry_run: bool = False,
):
    return housekeeping.run_housekeeping(
        guest,
        settings,
        node=node,
        cluster=cluster,
        lxc_id=lxc_id,
        name="guest",
        desired_alloy=base,
        catalogue=_catalogue(),
        script_id=_script_identity(guest),
        log_override=guest.log_override,
        dry_run=dry_run,
    )


def _compliant(guest: Guest, settings: GlobalSettings, base: DesiredAlloyConfig) -> None:
    """Make the guest fully compliant for the idle/cadence tests."""
    guest.alloy_config_sha = base.sha256
    guest.journald_hash = _journald_hash(settings)
    guest.alloy_env_hash = hashlib.sha256(
        housekeeping.journal_env_content(settings.housekeeping_local_retention_hours).encode()
    ).hexdigest()


def _prepare(
    guest: Guest,
    settings: GlobalSettings,
    base: DesiredAlloyConfig,
    *,
    dry_run: bool = False,
    allow_install: bool = False,
):
    return housekeeping.prepare_lxc_logging(
        guest,
        settings,
        node=NODE,
        cluster=CLUSTER,
        lxc_id=LXC,
        name="guest",
        base=base,
        catalogue=_catalogue(),
        script_id=_script_identity(guest),
        log_override=guest.log_override,
        dry_run=dry_run,
        allow_install=allow_install,
    )


# --------------------------------------------------------------------------- #
# Cluster-aware exclusions
# --------------------------------------------------------------------------- #


def test_general_exclusion_is_cluster_qualified(history: Path, guest: Guest):
    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(
            history, url=stub.url, exclude_list=["alpha/120"]
        )

        excluded = _run(guest, settings, base)
        assert guest.probe_calls == 0
        assert excluded.summary is None
        assert excluded.failed is False
        assert not (history / "housekeeping.sqlite3").exists()

        other = Guest(Path(guest.guest_root), Path(guest.spool))
        matched = _run(other, settings, base, cluster="beta")
    assert other.probe_calls >= 2
    assert matched.failed is False
    # No recognized identity: the sanitized coverage finding is reported and
    # the fleet base journal config (no app readers) is still enforced.
    assert matched.effective_alloy == base
    assert any("unsupported" in warning for warning in matched.warnings)


def test_housekeeping_exclusion_list_skips_without_touching_the_guest(history: Path, guest: Guest):
    base = _base("http://127.0.0.1:9")
    settings = _settings(history, lxc_housekeeping_exclude_list=["alpha/120"])
    result = _run(guest, settings, base)
    assert guest.probe_calls == 0
    assert guest.applied == []
    assert result.summary is None
    assert result.effective_alloy is None
    assert not (history / "housekeeping.sqlite3").exists()


def test_alloy_excluded_guest_warns_without_actions(history: Path, guest: Guest):
    base = _base("http://127.0.0.1:9")
    settings = _settings(
        history, url="http://127.0.0.1:9", lxc_alloy_exclude_list=["120"]
    )
    result = _run(guest, settings, base)
    assert guest.probe_calls == 0
    assert guest.applied == []
    assert result.summary is None
    assert result.failed is False
    assert result.effective_alloy is None
    assert any("excluded from Alloy" in warning for warning in result.warnings)


# --------------------------------------------------------------------------- #
# Stopped / template guests never create checkpoint state
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("stopped,template", [(True, False), (False, True)])
def test_stopped_and_template_guests_create_no_state(history: Path, guest: Guest, stopped: bool, template: bool):
    guest.running = not stopped
    guest.template = template
    base = _base("http://127.0.0.1:9")
    settings = _settings(history, url="http://127.0.0.1:9", housekeeping_enabled=True)

    result = _run(guest, settings, base)

    assert guest.probe_calls == 1
    assert guest.applied == []
    assert guest.capture_calls == 0 and guest.snapshot_calls == 0
    assert result.summary is None
    assert result.failed is False
    assert result.changed is False
    assert result.effective_alloy is None
    assert not (history / "housekeeping.sqlite3").exists()


# --------------------------------------------------------------------------- #
# Malformed probes fail closed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "override",
    [
        {"guest": {"name": "guest", "os_type": "debian", "is_running": True, "is_template": False}},
        {
            "guest": {"name": "guest", "os_type": "debian", "is_running": True, "is_template": False},
            "disk": {"total_bytes": 1, "available_bytes": 1, "used_percent": 1.0},
            "journal_bytes": 0,
            "profiles": [],
            "files": [{"path": "/data/logs/x.log", "compression": "zip"}],
            "cache_paths": {},
            "busy_tools": [],
            "binaries": {},
            "log_access_ready": True,
            "log_access_detail": {},
            "policy_sha256": {"journald": "", "alloy_env": "", "npm_logrotate": ""},
            "profile_evidence": {"npm": {}, "pbs": {}},
        },
    ],
)
def test_malformed_probe_fails_closed_without_mutation(history: Path, guest: Guest, override: Dict[str, Any]):
    guest.facts_override = override
    base = _base("http://127.0.0.1:9")
    settings = _settings(history, url="http://127.0.0.1:9", housekeeping_enabled=True)

    result = _run(guest, settings, base)

    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.effective_alloy is None
    assert guest.applied == []
    assert guest.capture_calls == 0
    assert not (history / "housekeeping.sqlite3").exists()


def _logging_request_fixture() -> Dict[str, Any]:
    return {
        "plugins": {
            "demoapp": {
                "layouts": [
                    {
                        "id": "default",
                        "required_units": [
                            {
                                "name": "demoapp.service",
                                "scope": "system",
                                "user": None,
                                "key": "system:demoapp.service",
                            }
                        ],
                        "running_units_any": [],
                    }
                ],
                "journal_units": [],
                "includes": {},
            }
        },
        "sources": {
            "demoapp/application": {
                "layouts": {
                    "default": {
                        "roots": ["/var/log/demoapp"],
                        "paths": ["/var/log/demoapp/*.log"],
                        "exact_files": [],
                        "exclude_paths": [],
                    }
                }
            }
        },
    }


def _logging_facts_fixture() -> Dict[str, Any]:
    return {
        "plugins": {
            "demoapp": {
                "units": {
                    "system:demoapp.service": {
                        "status": "active",
                        "instances": {},
                    }
                },
                "includes": {},
            }
        },
        "sources": {
            "demoapp/application": {
                "layouts": {
                    "default": {
                        "status": "ok",
                        "truncated": False,
                        "roots": {
                            "/var/log/demoapp": {
                                "exists": True,
                                "is_dir": True,
                                "is_symlink": False,
                                "device": 1,
                                "blocked_ancestor": False,
                                "status": "ok",
                                "access": {
                                    "traversable": True,
                                    "readable": True,
                                    "default_acl": True,
                                },
                            }
                        },
                        "exact_files": {},
                        "matches": [
                            {
                                "pattern": "/var/log/demoapp/*.log",
                                "matched": 1,
                                "skipped": 0,
                                "truncated": False,
                                "status": "ok",
                                "files": [
                                    {
                                        "path": "/var/log/demoapp/a.log",
                                        "device": 1,
                                        "inode": 2,
                                        "size": 3,
                                        "mtime_ns": 4,
                                        "accessible": True,
                                    }
                                ],
                            }
                        ],
                    }
                }
            }
        },
    }


def test_probe_housekeeping_forwards_logging_request_and_types_evidence(
    guest: Guest,
) -> None:
    request = _logging_request_fixture()
    facts = guest.facts()
    facts["logging"] = _logging_facts_fixture()
    guest.facts_override = facts

    probe = housekeeping.probe_housekeeping(
        guest, "120", logging_request=request
    )

    assert guest.logging_requests == [request]
    assert probe.logging == _logging_facts_fixture()


@pytest.mark.parametrize(
    "mutation",
    ["missing_source", "extra_unit", "bad_status", "missing_bool", "bad_file"],
)
def test_probe_housekeeping_rejects_mismatched_logging_evidence(
    guest: Guest, mutation: str
) -> None:
    request = _logging_request_fixture()
    logging = _logging_facts_fixture()
    if mutation == "missing_source":
        logging["sources"] = {}
    elif mutation == "extra_unit":
        logging["plugins"]["demoapp"]["units"]["system:extra.service"] = {
            "status": "active",
            "instances": {},
        }
    elif mutation == "bad_status":
        logging["plugins"]["demoapp"]["units"]["system:demoapp.service"][
            "status"
        ] = "running"
    elif mutation == "missing_bool":
        del logging["sources"]["demoapp/application"]["layouts"]["default"][
            "truncated"
        ]
    elif mutation == "bad_file":
        logging["sources"]["demoapp/application"]["layouts"]["default"]["matches"][
            0
        ]["files"][0]["device"] = "one"
    facts = guest.facts()
    facts["logging"] = logging
    guest.facts_override = facts

    with pytest.raises(housekeeping.HousekeepingError, match="logging"):
        housekeeping.probe_housekeeping(guest, "120", logging_request=request)


def test_probe_housekeeping_accepts_explicit_empty_logging_for_stopped_guest(
    guest: Guest,
) -> None:
    guest.running = False
    request = _logging_request_fixture()
    facts = guest.facts()
    guest.facts_override = facts

    probe = housekeeping.probe_housekeeping(
        guest, "120", logging_request=request
    )

    assert guest.logging_requests == [request]
    assert probe.logging == {"plugins": {}, "sources": {}}

    facts["logging"] = _logging_facts_fixture()
    with pytest.raises(
        housekeeping.HousekeepingError, match="stopped guest"
    ):
        housekeeping.probe_housekeeping(guest, "120", logging_request=request)


def test_probe_housekeeping_rejects_unsolicited_logging_without_request(
    guest: Guest,
) -> None:
    facts = guest.facts()
    facts["logging"] = _logging_facts_fixture()
    guest.facts_override = facts

    with pytest.raises(
        housekeeping.HousekeepingError, match="unsolicited logging"
    ):
        housekeeping.probe_housekeeping(guest, "120")


def test_probe_housekeeping_requires_the_logging_map(guest: Guest) -> None:
    facts = guest.facts()
    facts.pop("logging")
    guest.facts_override = facts

    with pytest.raises(
        housekeeping.HousekeepingError, match="incomplete facts"
    ):
        housekeeping.probe_housekeeping(guest, "120")


def test_probe_transport_failure_is_blocked(history: Path, guest: Guest):
    def boom(lxc_id: str, *, logging_request=None) -> PrimitiveResult:  # pragma: no cover - injected
        raise RuntimeError("ssh dropped")

    guest.housekeeping_probe = boom  # type: ignore[assignment]
    base = _base("http://127.0.0.1:9")
    settings = _settings(history, url="http://127.0.0.1:9", housekeeping_enabled=True)

    result = _run(guest, settings, base)

    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.effective_alloy is None
    assert not (history / "housekeeping.sqlite3").exists()


# --------------------------------------------------------------------------- #
# Corrupt checkpoints are preserved and fail closed
# --------------------------------------------------------------------------- #


def test_corrupt_checkpoint_is_preserved_and_blocks_mutation(history: Path, guest: Guest):
    db = history / "housekeeping.sqlite3"
    db.write_bytes(b"this is not a sqlite database" * 64)
    before = db.read_bytes()
    base = _base("http://127.0.0.1:9")
    settings = _settings(history, url="http://127.0.0.1:9", housekeeping_enabled=True)

    result = _run(guest, settings, base)

    assert db.read_bytes() == before
    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert guest.applied == []
    assert guest.capture_calls == 0
    assert any("retained" in warning for warning in result.warnings)


def test_schema_mismatch_checkpoint_is_preserved(history: Path, guest: Guest):
    db = history / "housekeeping.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    base = _base("http://127.0.0.1:9")
    settings = _settings(history, url="http://127.0.0.1:9", housekeeping_enabled=True)

    result = _run(guest, settings, base)

    assert db.read_bytes() == before
    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert guest.applied == []


# --------------------------------------------------------------------------- #
# Cache reclamation is independent of the archive/Loki surface
# --------------------------------------------------------------------------- #


def test_cache_reclamation_survives_missing_loki_url(history: Path, guest: Guest, tmp_path: Path):
    yarn = _cache_dir(tmp_path, "yarn")
    before = _measure(yarn)
    assert before > 0
    guest.cache_specs["yarn"] = CacheSpec("/usr/local/share/.cache/yarn/v6", yarn, "1.22.19")
    base = _base("http://127.0.0.1:9")
    settings = _settings(history)  # no URL configured

    result = _run(guest, settings, base)

    assert guest.applied == [hc.CACHE_CLEAN_COMMANDS["yarn"]]
    assert _measure(yarn) == 0
    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.summary.bytes_reclaimed == before
    assert any("Loki" in warning for warning in result.warnings)
    assert result.effective_alloy is None


def test_cache_reclamation_survives_missing_alloy(history: Path, guest: Guest, tmp_path: Path):
    yarn = _cache_dir(tmp_path, "yarn")
    before = _measure(yarn)
    guest.cache_specs["yarn"] = CacheSpec("/usr/local/share/.cache/yarn/v6", yarn, "1.22.19")
    guest.script_id = "nginxproxymanager"
    guest.alloy_binary = False
    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        result = _run(guest, settings, base)

    assert _measure(yarn) == 0
    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.summary.bytes_reclaimed == before
    assert any("Alloy is not installed" in warning for warning in result.warnings)
    assert result.effective_alloy is None


# --------------------------------------------------------------------------- #
# Blocked archival keeps the effective config and cache metrics
# --------------------------------------------------------------------------- #


def test_blocked_archival_keeps_effective_config_and_cache_metrics(history: Path, guest: Guest, tmp_path: Path):
    source = Path(guest.guest_root) / NPM_ARCHIVE.lstrip("/")
    source.write_bytes(b"old npm access line\n")
    guest.profiles = ["npm"]
    guest.files = [_wire(Path(guest.guest_root), NPM_ARCHIVE)]
    yarn = _cache_dir(tmp_path, "yarn")
    before = _measure(yarn)
    guest.cache_specs["yarn"] = CacheSpec("/usr/local/share/.cache/yarn/v6", yarn, "1.22.19")

    with LokiStub(push_status=503) as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        result = _run(guest, settings, base)

    # Cache reclamation is measured and survives the archive failure.
    assert _measure(yarn) == 0
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.summary.bytes_reclaimed == before
    assert result.summary.bytes_archived == 0
    assert result.failed is True
    # The generated per-guest file source survives so later reconciliation cannot
    # restore the journal-only base over it.
    assert result.effective_alloy is not None
    assert "fleet_npm" in result.effective_alloy.content
    # No deletion happens while delivery is unhealthy.
    assert guest.prune_calls == []
    assert source.exists()
    assert guest.capture_calls == 1


def test_blocked_retention_gate_keeps_cache_metrics(history: Path, guest: Guest, tmp_path: Path):
    yarn = _cache_dir(tmp_path, "yarn")
    before = _measure(yarn)
    guest.cache_specs["yarn"] = CacheSpec("/usr/local/share/.cache/yarn/v6", yarn, "1.22.19")

    with LokiStub(ready_status=503) as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        _compliant(guest, settings, base)
        guest.script_id = "nginxproxymanager"
        result = _run(guest, settings, base)

    # The retention deletion gate is closed (Loki not ready), but the cache
    # reclamation is real and must survive into the summary.
    assert _measure(yarn) == 0
    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.summary.bytes_reclaimed == before
    assert result.summary.files_pruned == 0
    assert guest.prune_calls == []


def test_pending_backfill_keeps_effective_config_and_archives_metrics(history: Path, guest: Guest):
    source = Path(guest.guest_root) / NPM_ARCHIVE.lstrip("/")
    source.write_bytes((b"x" * 512 + b"\n") * 4000)  # ~2 MiB > 1 MiB budget
    guest.profiles = ["npm"]
    guest.files = [_wire(Path(guest.guest_root), NPM_ARCHIVE)]

    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(
            history, url=stub.url, housekeeping_enabled=True, housekeeping_backfill_budget_mb=1
        )
        result = _run(guest, settings, base)

    assert result.failed is False
    assert result.summary is not None and result.summary.status == "Backfill pending"
    assert result.summary.bytes_archived > 0
    assert result.effective_alloy is not None and "fleet_npm" in result.effective_alloy.content
    assert guest.prune_calls == []
    assert source.exists()


def test_failed_configuration_is_blocked_not_configured(history: Path, guest: Guest):
    source = Path(guest.guest_root) / NPM_ARCHIVE.lstrip("/")
    source.write_bytes(b"line\n")
    guest.profiles = ["npm"]
    guest.files = [_wire(Path(guest.guest_root), NPM_ARCHIVE)]
    guest.alloy_reconcile_failed = True

    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        result = _run(guest, settings, base)

    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    # No inferred post-operation success, and no archival attempted after the
    # generation failed.
    assert guest.capture_calls == 0
    assert result.effective_alloy is None
    assert guest.validation_calls


def test_prepare_probe_failure_keeps_the_desired_base(history: Path, guest: Guest):
    guest.fail_probe_calls = {2}
    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        result = _run(guest, settings, base)

    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    # A failed preparation probe yields no verified config; the journal-only
    # base is never substituted for rejected evidence.
    assert result.effective_alloy is None


def test_prepare_probe_failure_preserves_known_file_sources(history: Path, guest: Guest):
    guest.profiles = ["npm"]
    guest.fail_probe_calls = {2}
    with LokiStub() as stub:
        result = _run(guest, _settings(history, url=stub.url, housekeeping_enabled=True), _base(stub.url))
    assert result.failed
    assert result.effective_alloy is None
    assert guest.capture_calls == 0
    assert guest.prune_calls == []


# --------------------------------------------------------------------------- #
# Dry run creates nothing and mutates nothing
# --------------------------------------------------------------------------- #


def test_dry_run_creates_no_state_and_mutates_no_guest(history: Path, guest: Guest, tmp_path: Path):
    source = Path(guest.guest_root) / NPM_ARCHIVE.lstrip("/")
    source.write_bytes(b"line\n")
    before_source = source.read_bytes()
    guest.profiles = ["npm"]
    guest.files = [_wire(Path(guest.guest_root), NPM_ARCHIVE)]
    yarn = _cache_dir(tmp_path, "yarn")
    before_yarn = _measure(yarn)
    guest.cache_specs["yarn"] = CacheSpec("/usr/local/share/.cache/yarn/v6", yarn, "1.22.19")

    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        result = _run(guest, settings, base, dry_run=True)

    assert not (history / "housekeeping.sqlite3").exists()
    assert guest.capture_calls == 0 and guest.snapshot_calls == 0
    assert _measure(yarn) == before_yarn
    assert source.read_bytes() == before_source
    assert result.changed is False
    assert result.summary is not None and result.summary.status == "Audit"
    assert result.effective_alloy is not None and "fleet_npm" in result.effective_alloy.content


# --------------------------------------------------------------------------- #
# Idle suppression and durable per-tool cadence
# --------------------------------------------------------------------------- #


def test_repeated_idle_maintenance_stays_silent(history: Path, guest: Guest):
    guest.log_override = GuestLogOverride(mode="disabled")
    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        _compliant(guest, settings, base)

        first = _run(guest, settings, base)
        second = _run(guest, settings, base)

    for result in (first, second):
        assert result.summary is None
        assert result.failed is False
        assert result.changed is False
        assert result.warnings == []
        assert result.effective_alloy is base


def test_successful_cache_clean_records_durable_per_tool_cadence(history: Path, guest: Guest, tmp_path: Path):
    yarn = _cache_dir(tmp_path, "yarn")
    guest.cache_specs["yarn"] = CacheSpec("/usr/local/share/.cache/yarn/v6", yarn, "1.22.19")

    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        _compliant(guest, settings, base)

        first = _run(guest, settings, base)
        assert first.summary is not None and first.summary.status == "Cleaned"
        assert first.summary.bytes_reclaimed > 0
        assert guest.cache_commands() == [hc.CACHE_CLEAN_COMMANDS["yarn"]]

        # Re-populate the cache and run again immediately: the durable 24h
        # cadence must suppress a second clean (the idempotent journal vacuum
        # still ticks each run, so compare cache actions, not the whole list).
        refilled = _cache_dir(tmp_path, "yarn")
        refilled_size = _measure(refilled)
        assert refilled_size > 0
        second = _run(guest, settings, base)

    assert guest.cache_commands() == [hc.CACHE_CLEAN_COMMANDS["yarn"]]
    assert _measure(refilled) == refilled_size
    assert second.summary is None
    assert second.changed is False

    with CheckpointStore.open(history / "housekeeping.sqlite3", read_only=True) as store:
        assert store.last_cache_clean(GuestKey(CLUSTER, NODE, LXC), "yarn") is not None


def test_missing_desired_alloy_blocks_without_touching_guest(history: Path, guest: Guest):
    with LokiStub() as stub:
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        result = _run(guest, settings, None)

    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.effective_alloy is None
    assert guest.applied == []


# --------------------------------------------------------------------------- #
# Scoped ACL preparation: one typed helper request, safe missing paths
# --------------------------------------------------------------------------- #


def test_log_acl_repair_tolerates_absent_exact_siblings(history: Path, guest: Guest):
    logs = Path(guest.guest_root) / "home/hermes/.hermes/logs"
    logs.mkdir(parents=True)
    (logs / "agent.log").write_text("agent\n")
    (logs / "errors.log").write_text("error\n")
    guest.script_id = "hermesagent"
    guest.access_ready = False

    result = _prepare(guest, _settings(history), _base("http://127.0.0.1:9"))

    assert result.failed is False
    assert result.summary is not None and result.summary.status == "Configured"
    assert result.effective_alloy is not None
    assert "fleet_" in result.effective_alloy.content
    # The two absent optional-mode files are skipped, never fabricated as read.
    assert guest.acl_files == {
        "/home/hermes/.hermes/logs/agent.log",
        "/home/hermes/.hermes/logs/errors.log",
    }
    assert "/home/hermes" in guest.acl_traverse
    assert "/home/hermes/.hermes" in guest.acl_traverse
    # No shell ACL command is ever passed through the action transport.
    assert not any("setfacl" in command for command in guest.applied)


def test_recreated_exact_file_is_repaired_on_next_maintenance(
    history: Path, guest: Guest
):
    logs = Path(guest.guest_root) / "home/hermes/.hermes/logs"
    logs.mkdir(parents=True)
    agent = logs / "agent.log"
    agent.write_text("agent\n")
    guest.script_id = "hermesagent"
    guest.access_ready = False

    first = _prepare(guest, _settings(history), _base("http://127.0.0.1:9"))
    assert first.failed is False
    assert "/home/hermes/.hermes/logs/agent.log" in guest.acl_files

    agent.unlink()
    agent.write_text("recreated\n")
    guest.acl_files.discard("/home/hermes/.hermes/logs/agent.log")

    second = _prepare(guest, _settings(history), _base("http://127.0.0.1:9"))
    assert second.failed is False
    assert second.summary is not None and second.summary.status == "Configured"
    assert second.effective_alloy is not None
    assert "fleet_" in second.effective_alloy.content
    assert guest.acl_files == {"/home/hermes/.hermes/logs/agent.log"}


def test_missing_exact_parent_blocks_before_acl_repair(history: Path, guest: Guest):
    guest.script_id = "uptimekuma"
    guest.access_ready = False

    result = _prepare(guest, _settings(history), _base("http://127.0.0.1:9"))

    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.effective_alloy is None
    assert guest.log_acl_calls == []
    assert guest.applied == []


def test_exact_file_uptime_kuma_error_log_never_grants_sibling_database(
    history: Path, guest: Guest
):
    data = Path(guest.guest_root) / "opt/uptime-kuma/data"
    data.mkdir(parents=True)
    error_log = data / "error.log"
    error_log.write_bytes(b"boom\n")
    error_log.chmod(0o600)
    database = data / "kuma.db"
    database.write_bytes(b"sqlite-secret")
    database.chmod(0o600)
    before_mode = stat.S_IMODE(database.stat().st_mode)
    before_bytes = database.read_bytes()

    guest.script_id = "uptimekuma"
    guest.access_ready = False
    base = _base("http://127.0.0.1:9")
    result = _prepare(guest, _settings(history), base)

    assert guest.acl_files == {"/opt/uptime-kuma/data/error.log"}
    assert "/opt/uptime-kuma/data" in guest.acl_traverse
    assert "/opt/uptime-kuma/data/kuma.db" not in guest.acl_files
    # Only the declared exact file becomes readable for Alloy; its sibling
    # database is never exposed.
    assert "setfacl" not in "\n".join(guest.applied)
    assert stat.S_IMODE(database.stat().st_mode) == before_mode
    assert database.read_bytes() == before_bytes
    assert result.failed is False
    assert result.effective_alloy is not None
    assert "fleet_" in result.effective_alloy.content


def test_invalid_candidate_syntax_blocks_before_acl_or_environment(
    history: Path, guest: Guest
):
    guest.profiles = ["npm"]
    guest.access_ready = False
    guest.validate_only_fails = True
    result = _prepare(guest, _settings(history), _base("http://127.0.0.1:9"))

    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.effective_alloy is None
    assert guest.validation_calls
    assert guest.applied == []
    assert guest.log_acl_calls == []
    assert guest.acl_roots == set()
    assert guest.acl_traverse == set()
    assert guest.acl_files == set()
    assert all(call["validate_only"] for call in guest.alloy_reconciles)


def _acl_bootstrap_guest(guest: Guest, settings: GlobalSettings) -> None:
    """Existing Alloy guest whose ACL tools are missing on the real helper."""
    guest.script_id = "nginxproxymanager"
    guest.binaries["setfacl"] = None
    guest.binaries["getfacl"] = None
    guest.access_ready = False
    guest.logging_source_status = {"nginxproxymanager/application": "unavailable"}
    guest.alloy_env_hash = hashlib.sha256(
        housekeeping.journal_env_content(
            settings.housekeeping_local_retention_hours
        ).encode()
    ).hexdigest()


def test_explicit_alloy_setup_installs_missing_acl_tools_then_repairs_access(
    history: Path, guest: Guest
):
    settings = _settings(history, url="http://127.0.0.1:9")
    base = _base("http://127.0.0.1:9")
    _acl_bootstrap_guest(guest, settings)

    result = _prepare(guest, settings, base, allow_install=True)

    assert result.summary is not None and result.summary.status == "Configured"
    assert result.failed is False
    assert result.effective_alloy is not None
    # The install-only step restored the ACL prerequisite; the re-probe then
    # observed the real (denied) access, which the scoped repair fixed.
    assert guest.binaries["setfacl"] and guest.binaries["getfacl"]
    assert guest.log_acl_calls == [{"roots": ("/data/logs",), "exact_files": ()}]
    assert "/data/logs" in guest.acl_roots
    assert guest.alloy_config_sha == result.effective_alloy.sha256
    install_calls = [call for call in guest.alloy_reconciles if call["install"]]
    assert len(install_calls) == 1
    assert install_calls[0]["configure"] is False


def test_recurring_preparation_never_installs_missing_acl_tools(
    history: Path, guest: Guest
):
    settings = _settings(history, url="http://127.0.0.1:9")
    base = _base("http://127.0.0.1:9")
    _acl_bootstrap_guest(guest, settings)

    result = _prepare(guest, settings, base, allow_install=False)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None
    assert any(
        "ACL tools missing" in warning and "--alloy-only" in warning
        for warning in result.warnings
    )
    assert guest.alloy_reconciles == []
    assert guest.log_acl_calls == []
    assert guest.applied == []
    assert guest.acl_roots == set()
    assert guest.binaries["setfacl"] is None
    assert guest.binaries["getfacl"] is None


def test_explicit_alloy_setup_install_failure_blocks_before_acl_or_environment(
    history: Path, guest: Guest
):
    settings = _settings(history, url="http://127.0.0.1:9")
    base = _base("http://127.0.0.1:9")
    _acl_bootstrap_guest(guest, settings)
    guest.alloy_reconcile_failed = True

    result = _prepare(guest, settings, base, allow_install=True)

    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.failed is True
    assert result.effective_alloy is None
    assert any("installation failed" in warning for warning in result.warnings)
    assert len(guest.alloy_reconciles) == 1
    assert guest.alloy_reconciles[0]["install"] is True
    assert guest.validation_calls == []
    assert guest.log_acl_calls == []
    assert guest.applied == []
    assert guest.alloy_config_sha == ""
    assert guest.acl_roots == set()
    assert guest.binaries["setfacl"] is None
    assert guest.binaries["getfacl"] is None


def _unknown_identity_guest(guest: Guest, *, binary: bool = True) -> None:
    """Resolved guest the catalogue cannot map to any application profile."""
    guest.script_id = "unmapped-service"
    guest.profiles = ["npm"]
    guest.alloy_binary = binary


def _desired_env_hash(settings: GlobalSettings) -> str:
    return hashlib.sha256(
        housekeeping.journal_env_content(
            settings.housekeeping_local_retention_hours
        ).encode()
    ).hexdigest()


def test_unknown_identity_replaces_stale_app_readers_with_base(
    history: Path, guest: Guest
):
    settings = _settings(history, url="http://127.0.0.1:9")
    base = _base("http://127.0.0.1:9")
    _unknown_identity_guest(guest)
    # An identity the manager cannot resolve still owns the guest's logging:
    # the previously generated application reader is replaced by the exact
    # fleet base journal config, not preserved and not guessed again.
    guest.alloy_config_sha = hashlib.sha256(
        b'loki.source.file "fleet_npm_application" {}\n'
    ).hexdigest()
    guest.alloy_env_hash = _desired_env_hash(settings)

    result = _prepare(guest, settings, base)

    assert result.failed is False
    assert result.summary is not None and result.summary.status == "Configured"
    assert result.effective_alloy == base
    assert guest.alloy_config_sha == base.sha256
    assert guest.validation_calls == [base.content]
    deployed = [call for call in guest.alloy_reconciles if not call["validate_only"]]
    assert len(deployed) == 1 and deployed[0]["configure"] is True
    # No application file sources or scoped file ACLs are fabricated for an
    # identity the catalogue cannot resolve.
    assert guest.log_acl_calls == []
    assert guest.acl_roots == set() and guest.acl_files == set()
    assert guest.applied == []
    assert "loki.source.file" not in base.content
    # Application coverage stays a diagnostic only.
    assert any("unsupported" in warning for warning in result.warnings)


def test_unknown_identity_alloy_only_installs_missing_alloy_then_enforces_base(
    history: Path, guest: Guest
):
    settings = _settings(history, url="http://127.0.0.1:9")
    base = _base("http://127.0.0.1:9")
    _unknown_identity_guest(guest, binary=False)

    result = _prepare(guest, settings, base, allow_install=True)

    assert result.failed is False
    assert result.summary is not None and result.summary.status == "Configured"
    assert result.effective_alloy == base
    installs = [call for call in guest.alloy_reconciles if call["install"]]
    assert len(installs) == 1
    assert installs[0]["configure"] is False
    assert guest.alloy_binary is True
    assert guest.alloy_config_sha == base.sha256
    assert guest.validation_calls == [base.content]
    assert guest.log_acl_calls == []
    assert guest.acl_roots == set() and guest.acl_files == set()
    # The real post-reconcile journal membership/service state is verified.
    assert guest.alloy_journal and guest.alloy_enabled and guest.alloy_active


def test_unknown_identity_missing_alloy_blocks_recurring_preparation(
    history: Path, guest: Guest
):
    settings = _settings(history, url="http://127.0.0.1:9")
    base = _base("http://127.0.0.1:9")
    _unknown_identity_guest(guest, binary=False)

    result = _prepare(guest, settings, base, allow_install=False)

    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.effective_alloy is None
    assert any("Alloy is not installed" in warning for warning in result.warnings)
    assert guest.alloy_reconciles == []
    assert guest.log_acl_calls == []
    assert guest.applied == []
    assert guest.alloy_binary is False


def test_unknown_identity_dry_run_reports_without_mutation(
    history: Path, guest: Guest
):
    settings = _settings(history, url="http://127.0.0.1:9")
    base = _base("http://127.0.0.1:9")
    _unknown_identity_guest(guest, binary=False)

    result = _prepare(guest, settings, base, dry_run=True)

    assert result.failed is False
    assert result.summary is not None and result.summary.status == "Audit"
    assert result.changed is False
    assert result.effective_alloy == base
    assert guest.alloy_reconciles == []
    assert guest.validation_calls == []
    assert guest.applied == []
    assert guest.log_acl_calls == []
    assert guest.alloy_binary is False
    assert any("unsupported" in warning for warning in result.warnings)
    assert any("Alloy is not installed" in warning for warning in result.warnings)


def test_unknown_identity_does_not_authorize_app_archive_or_prune(
    history: Path, guest: Guest
):
    source = Path(guest.guest_root) / NPM_ARCHIVE.lstrip("/")
    source.write_bytes(b"old npm access line\n")
    _unknown_identity_guest(guest)
    guest.files = [_wire(Path(guest.guest_root), NPM_ARCHIVE)]

    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        result = _run(guest, settings, base)

    assert result.failed is False
    assert result.effective_alloy == base
    # A profile the probe recognizes is not archive authority: the unresolved
    # selection yields no legacy eligibility, so capture, archival and file
    # pruning stay paused.
    assert guest.capture_calls == 0
    assert guest.prune_calls == []
    assert source.read_bytes() == b"old npm access line\n"
    assert any("archive work paused" in warning for warning in result.warnings)


def test_legacy_archive_eligibility_requires_default_complete_sources(
    history: Path, guest: Guest
):
    guest.profiles = ["npm"]
    selection = lp.select_log_plugins(
        _catalogue(), script_id="nginxproxymanager", override=None, auto=True
    )
    request = lp.build_logging_request(selection)
    logging = guest._logging_facts(request)
    resolved = lp.validate_log_layout(selection, logging)
    assert housekeeping.legacy_archive_profiles(selection, resolved) == frozenset({"npm"})

    remapped = lp.select_log_plugins(
        _catalogue(),
        script_id="nginxproxymanager",
        override=GuestLogOverride(
            mode="replace",
            plugins=["nginxproxymanager"],
            locations={"nginxproxymanager/application": ["/data/logs/*.log"]},
        ),
        auto=True,
    )
    remapped_resolved = lp.validate_log_layout(
        remapped, guest._logging_facts(lp.build_logging_request(remapped))
    )
    assert housekeeping.legacy_archive_profiles(remapped, remapped_resolved) == frozenset()

    disabled = lp.select_log_plugins(
        _catalogue(),
        script_id="nginxproxymanager",
        override=GuestLogOverride(
            mode="replace",
            plugins=["nginxproxymanager"],
            disable_sources=["nginxproxymanager/application"],
        ),
        auto=True,
    )
    disabled_resolved = lp.validate_log_layout(
        disabled, guest._logging_facts(lp.build_logging_request(disabled))
    )
    assert housekeeping.legacy_archive_profiles(disabled, disabled_resolved) == frozenset()


def test_recognized_but_ineligible_legacy_profile_pauses_importer(
    history: Path, guest: Guest
):
    source = Path(guest.guest_root) / NPM_ARCHIVE.lstrip("/")
    source.write_bytes(b"old npm access line\n")
    guest.profiles = ["npm"]
    guest.files = [_wire(Path(guest.guest_root), NPM_ARCHIVE)]
    guest.log_override = GuestLogOverride(mode="disabled")

    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        _compliant(guest, settings, base)
        result = _run(guest, settings, base)

    assert guest.capture_calls == 0
    assert guest.prune_calls == []
    assert source.read_bytes() == b"old npm access line\n"
    assert result.failed is False
    assert any("archive work paused" in warning for warning in result.warnings)
    assert result.effective_alloy is base
