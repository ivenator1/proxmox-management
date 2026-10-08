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

import hashlib
import http.server
import json
import os
import re
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
from proxmox_fleet.alloy import DesiredAlloyConfig
from proxmox_fleet.housekeeping_checkpoint import CheckpointStore, GuestKey
from proxmox_fleet.housekeeping_io import REQUIRED_BINARIES
from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.runner import PrimitiveResult

CLUSTER = "alpha"
NODE = "pve"
LXC = "120"
NPM_ARCHIVE = "/data/logs/access.log.1"

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
    return None


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
        # observability
        self.probe_calls = 0
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
            out[tool] = {
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

    def facts(self) -> Dict[str, Any]:
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
        }

    # -- transport --------------------------------------------------------- #

    def housekeeping_probe(self, lxc_id: str) -> PrimitiveResult:
        self.probe_calls += 1
        if self.probe_script:
            facts = self.probe_script.pop(0)
        elif self.facts_override is not None:
            facts = self.facts_override
        else:
            facts = self.facts()
        return PrimitiveResult(rc=0, facts=facts)

    def housekeeping_apply(self, lxc_id: str, *, command: str) -> PrimitiveResult:
        self.applied.append(command)
        tool = _tool_for_command(command)
        if tool is not None and tool in self.cache_specs:
            spec = self.cache_specs[tool]
            if spec.rc == 0:
                directory = spec.directory
                if directory is not None:
                    for path in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                        if path.is_file():
                            path.unlink()
            return PrimitiveResult(
                rc=spec.rc,
                changed=spec.rc == 0,
                failed=spec.rc != 0,
                stderr=("native failure" if spec.rc != 0 else ""),
                facts={},
            )
        from proxmox_fleet import housekeeping_native as native
        import shlex
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
    ) -> PrimitiveResult:
        self.alloy_reconciles.append(
            {
                "install": install,
                "configure": configure,
                "add_journal_group": add_journal_group,
                "repair_service": repair_service,
            }
        )
        if self.alloy_reconcile_failed:
            return PrimitiveResult(rc=1, failed=True, stderr="alloy deploy failed", facts={})
        self.alloy_binary = True
        self.alloy_config_sha = hashlib.sha256(desired_content.encode("utf-8")).hexdigest()
        return PrimitiveResult(rc=0, changed=True, facts={})

    # -- helpers ----------------------------------------------------------- #

    def cache_commands(self) -> List[str]:
        return [command for command in self.applied if command in hc.CACHE_CLEAN_COMMANDS.values()]


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
        dry_run=dry_run,
    )


def _compliant(guest: Guest, settings: GlobalSettings, base: DesiredAlloyConfig) -> None:
    """Make the guest fully compliant for the idle/cadence tests."""
    guest.alloy_config_sha = base.sha256
    guest.journald_hash = _journald_hash(settings)
    guest.alloy_env_hash = hashlib.sha256(
        housekeeping.journal_env_content(settings.housekeeping_local_retention_hours).encode()
    ).hexdigest()


# --------------------------------------------------------------------------- #
# Cluster-aware exclusions
# --------------------------------------------------------------------------- #


def test_general_exclusion_is_cluster_qualified(history: Path, guest: Guest):
    base = _base("http://127.0.0.1:9")
    settings = _settings(history, exclude_list=["alpha/120"])

    excluded = _run(guest, settings, base)
    assert guest.probe_calls == 0
    assert excluded.summary is None
    assert excluded.failed is False
    assert not (history / "housekeeping.sqlite3").exists()

    other = Guest(Path(guest.guest_root), Path(guest.spool))
    other.probe_script = [other.facts()]
    matched = _run(other, settings, base, cluster="beta")
    assert other.probe_calls == 1
    assert matched.effective_alloy is base


def test_housekeeping_exclusion_list_skips_without_touching_the_guest(history: Path, guest: Guest):
    base = _base("http://127.0.0.1:9")
    settings = _settings(history, lxc_housekeeping_exclude_list=["alpha/120"])
    result = _run(guest, settings, base)
    assert guest.probe_calls == 0
    assert guest.applied == []
    assert result.summary is None
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
    assert result.effective_alloy is base
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
    assert result.effective_alloy is base
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
    assert result.effective_alloy is base
    assert guest.applied == []
    assert guest.capture_calls == 0
    assert not (history / "housekeeping.sqlite3").exists()


def test_probe_transport_failure_is_blocked(history: Path, guest: Guest):
    def boom(lxc_id: str) -> PrimitiveResult:  # pragma: no cover - injected
        raise RuntimeError("ssh dropped")

    guest.housekeeping_probe = boom  # type: ignore[assignment]
    base = _base("http://127.0.0.1:9")
    settings = _settings(history, url="http://127.0.0.1:9", housekeeping_enabled=True)

    result = _run(guest, settings, base)

    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    assert result.effective_alloy is base
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
    assert result.effective_alloy is base


def test_cache_reclamation_survives_missing_alloy(history: Path, guest: Guest, tmp_path: Path):
    yarn = _cache_dir(tmp_path, "yarn")
    before = _measure(yarn)
    guest.cache_specs["yarn"] = CacheSpec("/usr/local/share/.cache/yarn/v6", yarn, "1.22.19")
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
    assert result.effective_alloy is base


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
    assert result.effective_alloy is not None and "fleet_npm" in result.effective_alloy.content


def test_prepare_probe_failure_keeps_the_desired_base(history: Path, guest: Guest):
    guest.probe_script = [guest.facts(), {"guest": {"name": "guest"}}]
    with LokiStub() as stub:
        base = _base(stub.url)
        settings = _settings(history, url=stub.url, housekeeping_enabled=True)
        result = _run(guest, settings, base)

    assert result.failed is True
    assert result.summary is not None and result.summary.status == "Blocked"
    # The logging preparation probe failed, but the base is still the effective
    # config -- it must never be clobbered with None.
    assert result.effective_alloy is base


def test_prepare_probe_failure_preserves_known_file_sources(history: Path, guest: Guest):
    guest.profiles = ["npm"]
    guest.probe_script = [guest.facts(), {"guest": {"name": "guest"}}]
    with LokiStub() as stub:
        result = _run(guest, _settings(history, url=stub.url, housekeeping_enabled=True), _base(stub.url))
    assert result.failed
    assert result.effective_alloy is not None
    assert "fleet_npm" in result.effective_alloy.content
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
