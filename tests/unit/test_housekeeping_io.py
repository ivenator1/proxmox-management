"""Real-filesystem regressions for the step-2 housekeeping transport helper.

Every test drives ``proxmox_fleet.housekeeping_io`` against a temporary sysroot
tree (never a mocked filesystem), plus a deterministic command runner so tool
output is stable on any host.  ACL regressions additionally use the host's real
``setfacl``/``getfacl`` on temporary owned files with a private numeric UID and
skip when the host or filesystem cannot provide POSIX ACLs.  No test requires
root and none touches a real guest.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from proxmox_fleet import housekeeping_io as hio
from proxmox_fleet import log_plugins as lp
from proxmox_fleet.housekeeping import HousekeepingError, _validate_logging_facts

CAPTURE_ID = "a" * 64 + "/" + "b" * 32
NAMESPACE = "a" * 64
ACL_TEXT = (
    "# file: x\n"
    "user::rwx\n"
    "group::r-x\n"
    "other::r-x\n"
    "default:user::rwx\n"
    "default:group::r-x\n"
    "default:user:alloy:r-x\n"
    "default:mask::r-x\n"
    "default:group:alloy:r-x\n"
    "default:other::r-x\n"
)
MODULE = Path(hio.__file__)


# --------------------------------------------------------------------------- #
# fixture builders
# --------------------------------------------------------------------------- #


class FakeRunner:
    """Deterministic stand-in for the sysroot command runner (prefix matched)."""

    def __init__(self, mapping=None, default=None):
        self.mapping = {key: tuple(value) for key, value in (mapping or {}).items()}
        self.default = default
        self.calls = []

    def __call__(self, argv):
        key = " ".join(argv)
        self.calls.append(key)
        for prefix, result in self.mapping.items():
            if key.startswith(prefix):
                return result
        return self.default


def _write(root: Path, path: str, data, mode: int = 0o644) -> Path:
    target = root / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        target.write_bytes(data)
    else:
        target.write_text(data)
    target.chmod(mode)
    return target


def _mkdir(root: Path, path: str, mode: int = 0o755) -> Path:
    target = root / path.lstrip("/")
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(mode)
    return target


def _add_proc(root: Path, pid: int, argv) -> None:
    _mkdir(root, f"/proc/{pid}", 0o755)
    _write(root, f"/proc/{pid}/cmdline", b"\0".join(a.encode() for a in argv) + b"\0")
    _mkdir(root, f"/proc/{pid}/fd", 0o755)
    _mkdir(root, f"/proc/{pid}/fdinfo", 0o755)


def _add_fd(root: Path, pid: int, number: int, target: Path, *, writable: bool) -> None:
    link = root / "proc" / str(pid) / "fd" / str(number)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    flags = 0o100002 if writable else 0o100000
    _write(root, f"/proc/{pid}/fdinfo/{number}", f"pos:\t0\nflags:\t{flags:o}\n")


def _add_denied_proc(root: Path, pid: int, *, state: str, thread_states=None) -> None:
    """A ``/proc`` entry whose ``fd`` directory refuses enumeration.

    Mirrors the real kernel surfaces for a non-dumpable task: ``fd`` is
    root-owned and denied while ``stat``/``task`` stay world-readable.  ``state``
    is the leader state; ``thread_states`` maps every TID to its state.
    """
    _mkdir(root, f"/proc/{pid}", 0o755)
    _write(root, f"/proc/{pid}/cmdline", b"\0")
    _mkdir(root, f"/proc/{pid}/fd", 0o500)
    _mkdir(root, f"/proc/{pid}/fdinfo", 0o555)
    _write(root, f"/proc/{pid}/stat", f"{pid} (logwriter) {state} 1 {pid} 0 -1 0\n")
    for tid, tstate in (thread_states or {pid: state}).items():
        _mkdir(root, f"/proc/{pid}/task/{tid}", 0o755)
        _write(root, f"/proc/{pid}/task/{tid}/stat", f"{tid} (logwriter) {tstate} 1 {tid} 0 -1 0\n")


def _deny_fd_enumeration(monkeypatch, pids) -> None:
    real_listdir = os.listdir
    suffixes = tuple(f"/proc/{pid}/fd" for pid in pids)

    def guard(path="."):
        text = str(path).rstrip("/")
        if text.endswith(suffixes):
            raise PermissionError(13, "Permission denied", text)
        return real_listdir(path)

    monkeypatch.setattr(hio.os, "listdir", guard)


@pytest.fixture
def guest(tmp_path: Path) -> Path:
    root = tmp_path / "guest"
    root.mkdir()
    root = root.resolve()
    _write(root, "/etc/hostname", "npm-ct\n")
    _write(root, "/etc/os-release", 'ID=debian\nVERSION_ID="12"\n')
    _write(
        root,
        "/etc/passwd",
        "root:x:0:0:root:/root:/bin/bash\n"
        "alloy:x:991:991::/var/lib/alloy:/usr/sbin/nologin\n",
    )
    _write(root, "/etc/group", "root:x:0:\nalloy:x:991:\n")
    _add_proc(root, 1, ["init"])
    _add_proc(root, 4242, ["apt-get", "install", "-y", "acl"])
    return root


def _add_npm(root: Path) -> None:
    _write(root, "/etc/systemd/system/npm.service", "[Unit]\n")
    _write(root, "/etc/systemd/system/openresty.service", "[Unit]\n")
    _mkdir(root, "/opt/nginxproxymanager", 0o755)
    _mkdir(root, "/data/logs", 0o755)
    _mkdir(root, "/data/logs/letsencrypt", 0o755)
    _write(root, "/data/logs/access.log", "live access\n")
    _write(root, "/data/logs/error.log", "live error\n")
    _write(root, "/data/logs/backend.log", "live backend\n")
    _write(root, "/data/logs/access.log.1", "old access\n")
    _write(root, "/data/logs/access.log-20260101T000000.gz", gzip.compress(b"gz access\n"))
    _write(root, "/data/logs/letsencrypt/letsencrypt.log", "le log\n")
    _write(root, "/data/logs/notes.txt", "ignore me\n")


PBS_UPID_ACTIVE = "UPID:pbs:0001A2B3:00000000:00000000:00000000:root@pam:"
PBS_UPID_IDLE = "UPID:pbs:0004D5E6:00000000:00000000:00000000:root@pam:"


def _add_pbs(root: Path) -> None:
    _write(root, "/usr/bin/proxmox-backup-manager", "not executable here\n", 0o755)
    _write(root, "/etc/systemd/system/proxmox-backup-proxy.service", "[Unit]\n")
    _mkdir(root, "/var/log/proxmox-backup/tasks", 0o755)
    _mkdir(root, "/var/log/proxmox-backup/api", 0o755)
    _mkdir(root, "/var/log/proxmox-backup/tasks/AB", 0o755)
    _write(root, f"/var/log/proxmox-backup/tasks/AB/{PBS_UPID_ACTIVE}", "task output\n")
    _mkdir(root, "/var/log/proxmox-backup/tasks/CD", 0o755)
    _write(root, f"/var/log/proxmox-backup/tasks/CD/{PBS_UPID_IDLE}", "other output\n")
    _write(root, "/var/log/proxmox-backup/tasks/active", PBS_UPID_ACTIVE + "\n")
    _write(root, "/var/log/proxmox-backup/tasks/archive", "archive index\n")
    _write(root, "/var/log/proxmox-backup/tasks/archive.1", "archive index 1\n")
    _write(root, "/var/log/proxmox-backup/tasks/.active.lock", "")
    _write(root, "/var/log/proxmox-backup/api/access.log", "api access\n")
    _write(root, "/var/log/proxmox-backup/api/auth.log", "api auth\n")
    _write(root, "/var/log/proxmox-backup/api/access.log.1", "api access old\n")
    _write(root, "/var/log/proxmox-backup/api/access.log.2.zst", b"\x28\xb5\x2f\xfdstub")


def _add_acl_tools(root: Path) -> None:
    """Presence-only getfacl/setfacl (behaviour comes from an injected runner)."""
    _write(root, "/usr/bin/getfacl", "#!/bin/sh\nexit 1\n", 0o755)
    _write(root, "/usr/bin/setfacl", "#!/bin/sh\nexit 0\n", 0o755)


def _acl_runner(acl: str = ACL_TEXT) -> FakeRunner:
    defaults_only = "\n".join(
        line[len("default:"):] for line in acl.splitlines() if line.startswith("default:")
    ) + "\n"
    return FakeRunner(
        {
            "getfacl -p -d ": (0, defaults_only, ""),
            "getfacl -p ": (0, acl, ""),
        },
        default=(1, "", "missing"),
    )


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


def test_probe_npm_identity_active_writer_and_facts(guest: Path) -> None:
    _add_npm(guest)
    _add_acl_tools(guest)
    access = guest / "data/logs/access.log"
    _add_fd(guest, 1, 3, access, writable=True)
    facts = hio.probe({}, sysroot=str(guest), runner=_acl_runner())

    assert facts["guest"] == {
        "name": "npm-ct",
        "os_type": "debian",
        "is_running": True,
        "is_template": False,
    }
    assert facts["profiles"] == ["npm"]
    assert facts["disk"]["total_bytes"] > 0
    assert set(facts["policy_sha256"]) == {"journald", "alloy_env", "npm_logrotate"}
    assert all(value == "" for value in facts["policy_sha256"].values())
    assert facts["profile_evidence"]["npm"]["detected"] is True
    assert "apt" in facts["busy_tools"]

    by_path = {entry["path"]: entry for entry in facts["files"]}
    live = by_path["/data/logs/access.log"]
    st = access.lstat()
    assert live["is_active"] is True
    assert live["log_kind"] == "application"
    assert live["compression"] == "plain"
    assert live["device"] == st.st_dev
    assert live["inode"] == st.st_ino
    assert live["size"] == st.st_size
    assert live["mtime_ns"] == st.st_mtime_ns
    assert live["allocated_bytes"] == st.st_blocks * 512
    assert by_path["/data/logs/access.log.1"]["is_active"] is False
    assert by_path["/data/logs/access.log-20260101T000000.gz"]["compression"] == "gzip"
    assert by_path["/data/logs/letsencrypt/letsencrypt.log"]["log_kind"] == "application"
    assert "/data/logs/notes.txt" not in by_path


def test_probe_pbs_profile_tasks_api_and_active_index(guest: Path) -> None:
    _add_pbs(guest)
    facts = hio.probe({}, sysroot=str(guest), runner=FakeRunner())

    assert facts["profiles"] == ["pbs"]
    assert facts["binaries"]["proxmox-backup-manager"].endswith(
        "/usr/bin/proxmox-backup-manager"
    )
    by_path = {entry["path"]: entry for entry in facts["files"]}
    active = f"/var/log/proxmox-backup/tasks/AB/{PBS_UPID_ACTIVE}"
    idle = f"/var/log/proxmox-backup/tasks/CD/{PBS_UPID_IDLE}"
    assert by_path[active]["is_active"] is True
    assert by_path[active]["log_kind"] == "task"
    assert by_path[idle]["is_active"] is False
    assert by_path["/var/log/proxmox-backup/tasks/archive"]["log_kind"] == "task_index"
    assert by_path["/var/log/proxmox-backup/tasks/archive.1"]["log_kind"] == "task_index"
    assert "/var/log/proxmox-backup/tasks/active" not in by_path
    assert "/var/log/proxmox-backup/tasks/.active.lock" not in by_path
    assert by_path["/var/log/proxmox-backup/api/access.log"]["log_kind"] == "api"
    assert by_path["/var/log/proxmox-backup/api/access.log.1"]["compression"] == "plain"
    assert by_path["/var/log/proxmox-backup/api/access.log.2.zst"]["compression"] == "zstd"


def test_probe_partial_evidence_is_not_a_profile(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/app.log", "some other app\n")
    facts = hio.probe({}, sysroot=str(guest), runner=FakeRunner())

    assert facts["profiles"] == []
    assert facts["files"] == []
    assert facts["profile_evidence"]["npm"]["log_root"] is True
    assert facts["profile_evidence"]["npm"]["detected"] is False
    assert facts["log_access_ready"] is False
    assert facts["log_access_detail"]["dirs"] == {}


def test_probe_guest_facts_override_accepts_pct_keys(guest: Path) -> None:
    facts = hio.probe(
        {"guest": {"hostname": "ct123", "ostype": "debian", "status": "running", "template": 0}},
        sysroot=str(guest),
        runner=FakeRunner(),
    )
    assert facts["guest"] == {
        "name": "ct123",
        "os_type": "debian",
        "is_running": True,
        "is_template": False,
    }


def test_probe_rejects_failed_enumeration(monkeypatch, guest: Path) -> None:
    _add_npm(guest)
    real_scandir = os.scandir

    def fake_scandir(path=".", *args, **kwargs):
        if str(path).rstrip("/").endswith("data/logs"):
            raise PermissionError(13, "denied", str(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(hio.os, "scandir", fake_scandir)
    with pytest.raises(hio.HelperError):
        hio.probe({}, sysroot=str(guest), runner=FakeRunner())


def test_probe_requires_a_visible_process_table(guest: Path) -> None:
    empty = guest / "proc"
    for child in sorted(empty.iterdir()):
        for nested in sorted(child.rglob("*"), reverse=True):
            nested.unlink() if nested.is_file() or nested.is_symlink() else nested.rmdir()
        child.rmdir()
    with pytest.raises(hio.HelperError):
        hio.probe({}, sysroot=str(guest), runner=FakeRunner())
    os.rmdir(empty)
    with pytest.raises(hio.HelperError):
        hio.probe({}, sysroot=str(guest), runner=FakeRunner())


def test_probe_skips_a_denied_process_after_verified_disappearance(
    monkeypatch, guest: Path
) -> None:
    """A denied ``fd`` listing for a vanished process is not a live writer.

    ``EACCES`` on ``/proc/<pid>/fd`` is *not* "no writers": a live, non-dumpable
    process keeps a denied descriptor table.  But once the task is gone the
    kernel has already released its file table, so re-checking the same PID after
    the denial is the only safe way to tell a departed process from an
    uninspectable live writer.
    """
    _add_npm(guest)
    _mkdir(guest, "/proc/7171")
    _write(guest, "/proc/7171/cmdline", b"\0")
    _mkdir(guest, "/proc/7171/fd", 0o500)
    _deny_fd_enumeration(monkeypatch, [7171])
    facts = hio.probe({}, sysroot=str(guest), runner=_acl_runner())
    assert facts["profiles"] == ["npm"]


def test_probe_skips_a_denied_process_with_a_fully_dead_thread_group(
    monkeypatch, guest: Path
) -> None:
    _add_npm(guest)
    _add_denied_proc(guest, 5150, state="Z")
    _deny_fd_enumeration(monkeypatch, [5150])
    facts = hio.probe({}, sysroot=str(guest), runner=_acl_runner())
    assert facts["profiles"] == ["npm"]


def test_probe_refuses_a_denied_live_process(monkeypatch, guest: Path) -> None:
    _add_npm(guest)
    _add_denied_proc(guest, 6161, state="S")
    _deny_fd_enumeration(monkeypatch, [6161])
    with pytest.raises(hio.HelperError, match="cannot enumerate"):
        hio.probe({}, sysroot=str(guest), runner=_acl_runner())


@pytest.mark.parametrize("denied", [True, False])
def test_probe_refuses_a_dead_leader_with_a_live_sibling_thread(
    monkeypatch, guest: Path, denied: bool
) -> None:
    _add_npm(guest)
    _add_denied_proc(guest, 6262, state="Z", thread_states={6262: "Z", 6263: "S"})
    if denied:
        _deny_fd_enumeration(monkeypatch, [6262])
    with pytest.raises(hio.HelperError):
        hio.probe({}, sysroot=str(guest), runner=_acl_runner())


def test_probe_refuses_a_denied_process_with_incomplete_task_enumeration(
    monkeypatch, guest: Path
) -> None:
    _add_npm(guest)
    _mkdir(guest, "/proc/6363")
    _write(guest, "/proc/6363/cmdline", b"\0")
    _mkdir(guest, "/proc/6363/fd", 0o500)
    _write(guest, "/proc/6363/stat", b"6363 (logwriter) Z 1 6363 0 -1 0\n")
    _deny_fd_enumeration(monkeypatch, [6363])
    with pytest.raises(hio.HelperError, match="cannot enumerate"):
        hio.probe({}, sysroot=str(guest), runner=_acl_runner())


def test_probe_refuses_a_dead_leader_when_a_thread_state_vanishes(
    monkeypatch, guest: Path
) -> None:
    _add_npm(guest)
    _add_denied_proc(guest, 6565, state="Z", thread_states={6565: "Z"})
    _mkdir(guest, "/proc/6565/task/6566")
    _deny_fd_enumeration(monkeypatch, [6565])
    with pytest.raises(hio.HelperError, match="cannot enumerate"):
        hio.probe({}, sysroot=str(guest), runner=_acl_runner())


def test_probe_refuses_a_denied_process_with_unknown_task_state(
    monkeypatch, guest: Path
) -> None:
    _add_npm(guest)
    _mkdir(guest, "/proc/6464")
    _write(guest, "/proc/6464/cmdline", b"\0")
    _mkdir(guest, "/proc/6464/fd", 0o500)
    _write(guest, "/proc/6464/stat", b"malformed without a close paren\n")
    _deny_fd_enumeration(monkeypatch, [6464])
    with pytest.raises(hio.HelperError, match="cannot enumerate"):
        hio.probe({}, sysroot=str(guest), runner=_acl_runner())


def test_probe_stopped_guest_skips_proc_and_never_starts(guest: Path) -> None:
    _add_npm(guest)
    for child in sorted((guest / "proc").iterdir()):
        for nested in sorted(child.rglob("*"), reverse=True):
            nested.unlink() if nested.is_file() or nested.is_symlink() else nested.rmdir()
        child.rmdir()
    facts = hio.probe(
        {"guest": {"name": "npm-ct", "os_type": "debian", "is_running": False, "is_template": False}},
        sysroot=str(guest),
        runner=_acl_runner(),
    )
    assert facts["guest"]["is_running"] is False
    assert facts["busy_tools"] == []
    assert len(facts["files"]) >= 5
    assert all(entry["is_active"] is False for entry in facts["files"])


def test_probe_cache_resolution_dependency_and_symlink_escape(guest: Path) -> None:
    runner = FakeRunner(
        {
            "yarn cache dir": (0, "/usr/local/share/.cache/yarn/v6\n", ""),
            "yarn --version": (0, "1.22.19\n", ""),
            "npm config get cache": (0, "/root/.npm\n", ""),
            "npm --version": (0, "10.5.0\n", ""),
        }
    )
    _mkdir(guest, "/usr/local/share/.cache/yarn/v6/pkg", 0o755)
    _write(guest, "/usr/local/share/.cache/yarn/v6/pkg/blob.bin", b"x" * 4096)
    _mkdir(guest, "/root/.npm/_cacache", 0o755)
    _mkdir(guest, "/app/node_modules", 0o755)
    (guest / "app/node_modules/leftpad").symlink_to(
        "../../usr/local/share/.cache/yarn/v6/pkg"
    )
    _mkdir(guest, "/srv/elsewhere", 0o755)
    (guest / "root/.local/share/pnpm").mkdir(parents=True, exist_ok=True)
    (guest / "root/.local/share/pnpm/store").symlink_to("../../../../srv/elsewhere")

    facts = hio.probe({}, sysroot=str(guest), runner=runner)

    yarn = facts["cache_paths"]["yarn"]
    assert yarn["root"] == "/usr/local/share/.cache/yarn/v6"
    assert yarn["source"] == "tool"
    assert yarn["resolved"] == os.path.realpath(str(guest / "usr/local/share/.cache/yarn/v6"))
    assert yarn["exists"] is True
    assert yarn["allowlisted"] is True
    assert yarn["escaped"] is False
    assert yarn["allocated_bytes"] >= 4096
    assert yarn["version"] == "1.22.19"
    assert yarn["dependency_referenced"] is True

    npm = facts["cache_paths"]["npm"]
    assert npm["allowlisted"] is True
    assert npm["version"] == "10.5.0"
    assert npm["allocated_bytes"] >= 0

    pnpm = facts["cache_paths"]["pnpm"]
    assert pnpm["escaped"] is True
    assert pnpm["allowlisted"] is False

    apt = facts["cache_paths"]["apt"]
    assert apt["root"] is None
    assert apt["exists"] is False
    assert apt["allocated_bytes"] == 0


def test_probe_bun_cache_facts_use_fixed_binary_and_native_help(guest: Path) -> None:
    runner = FakeRunner(
        {
            "bun pm cache": (0, "/root/.bun/install/cache\n", ""),
            "bun --version": (0, "1.4.4\n", ""),
            "bun pm --help": (
                0,
                "Commands:\n  bun pm cache prune --max-age <days>  remove old entries\n",
                "",
            ),
        }
    )
    _write(guest, "/usr/local/bin/bun", "#!/bin/sh\nexit 0\n", 0o755)
    _mkdir(guest, "/root/.bun/install/cache", 0o755)
    _mkdir(guest, "/root/.bun/install/cache/old-pkg", 0o755)
    blob = _write(guest, "/root/.bun/install/cache/old-pkg/blob.bin", b"x" * 8192)
    _mkdir(guest, "/root/.bun/install/cache/links", 0o755)
    kept = _write(guest, "/root/.bun/install/cache/links/kept.bin", b"k" * 4096)

    facts = hio.probe({}, sysroot=str(guest), runner=runner)
    bun = facts["cache_paths"]["bun"]

    assert bun["root"] == "/root/.bun/install/cache"
    assert bun["source"] == "tool"
    assert bun["resolved"] == os.path.realpath(str(guest / "root/.bun/install/cache"))
    assert bun["allowlisted"] is True
    assert bun["escaped"] is False
    assert bun["exists"] is True
    assert bun["binary"] == "/usr/local/bin/bun"
    assert bun["cache_prune_supported"] is True
    assert bun["version"] == "1.4.4"
    assert bun["error"] is None
    blob_bytes = os.lstat(blob).st_blocks * 512
    kept_bytes = os.lstat(kept).st_blocks * 512
    assert bun["allocated_bytes"] == blob_bytes + kept_bytes
    assert bun["reclaimable_bytes"] == blob_bytes


def test_probe_bun_without_fixed_binary_reports_no_prune_support(guest: Path) -> None:
    runner = FakeRunner(
        {
            "bun pm cache": (0, "/root/.bun/install/cache\n", ""),
            "bun --version": (0, "1.4.4\n", ""),
            "bun pm --help": (0, "bun pm cache prune --max-age <days>\n", ""),
        }
    )
    _mkdir(guest, "/root/.bun/install/cache", 0o755)
    _write(guest, "/root/.bun/install/cache/old.bin", b"x" * 4096)

    bun = hio.probe({}, sysroot=str(guest), runner=runner)["cache_paths"]["bun"]

    assert bun["binary"] is None
    assert bun["cache_prune_supported"] is False
    assert bun["reclaimable_bytes"] >= 4096


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ((0, "Usage: bun pm\n  bun pm cache prune --max-age <days>\n", ""), True),
        (
            (
                0,
                "\x1b[1mUsage\x1b[0m bun pm\n"
                "\x1b[36mbun\x1b[0m pm cache prune \x1b[33m--max-age\x1b[0m 30d\n",
                "",
            ),
            True,
        ),
        ((0, "  bun pm cache rm\n  --max-age 30\n", ""), False),
        ((0, "  bunx pm cache prune --max-age 30\n", ""), False),
        ((0, "  bun pm cache prune\n", ""), False),
        ((0, "  bun pm cache --max-age 30\n", ""), False),
        ((1, "  bun pm cache prune --max-age 30\n", "boom"), False),
    ],
)
def test_bun_prune_support_requires_anchored_rc0_help(result, expected) -> None:
    assert hio._bun_prune_supported(FakeRunner({"bun pm --help": result})) is expected


def test_bun_prune_support_rejects_missing_or_oversized_help() -> None:
    assert hio._bun_prune_supported(FakeRunner()) is False
    oversized = "bun pm cache prune --max-age 30\n" + "x" * hio.MAX_BUN_HELP_BYTES
    assert hio._bun_prune_supported(FakeRunner({"bun pm --help": (0, oversized, "")})) is False


def test_bun_cache_usage_counts_allocated_but_excludes_links_and_hardlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cache"
    (root / "links").mkdir(parents=True)
    (root / "sub").mkdir()
    plain = root / "plain.bin"
    plain.write_bytes(b"x" * 4096)
    nested = root / "sub" / "nested.bin"
    nested.write_bytes(b"x" * 2048)
    kept = root / "links" / "kept.bin"
    kept.write_bytes(b"k" * 8192)
    hard = root / "hard.bin"
    hard.write_bytes(b"h" * 8192)
    os.link(hard, tmp_path / "external.bin")
    sparse = root / "sparse.bin"
    with open(sparse, "wb") as handle:
        handle.seek(1024 * 1024 - 1)
        handle.write(b"\0")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"o" * 16384)
    (root / "escape").symlink_to(outside, target_is_directory=True)

    allocated, reclaimable, error = hio._bun_cache_usage(str(root))

    assert error is None
    expected_allocated = sum(
        os.lstat(path).st_blocks * 512
        for path in (plain, nested, kept, hard, sparse)
    )
    expected_reclaimable = sum(
        os.lstat(path).st_blocks * 512 for path in (plain, nested, sparse)
    )
    assert allocated == expected_allocated
    assert reclaimable == expected_reclaimable


def test_bun_cache_usage_fails_closed_when_the_root_is_not_a_directory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "not-a-directory"
    path.write_bytes(b"x")

    allocated, reclaimable, error = hio._bun_cache_usage(str(path))

    assert allocated is None
    assert reclaimable is None
    assert error is not None and "cannot open" in error


def test_bun_dependency_scan_ignores_only_the_retained_store_links(guest: Path) -> None:
    cache = _mkdir(guest, "/root/.bun/install/cache", 0o755)
    _mkdir(guest, "/root/.bun/install/cache/links/pkg", 0o755)
    _mkdir(guest, "/root/.bun/install/cache/pkg", 0o755)
    _mkdir(guest, "/app/node_modules", 0o755)
    (guest / "app/node_modules/linked").symlink_to(cache / "links" / "pkg")
    real = os.path.realpath(str(cache))

    # Bun's own global-store links are preserved by prune; any other cache
    # reference still blocks it, and other tools keep their stricter policy.
    assert hio._cache_is_dependency(str(guest), real, tool="bun") is False
    assert hio._cache_is_dependency(str(guest), real, tool="pnpm") is True

    _mkdir(guest, "/app/node_modules/.bun", 0o755)
    (guest / "app/node_modules/.bun/direct").symlink_to(cache / "pkg")
    assert hio._cache_is_dependency(str(guest), real, tool="bun") is True


def test_bun_probe_preserves_package_directory_atime(guest: Path, tmp_path: Path) -> None:
    """The measured consumer must never refresh an age native prune relies on."""
    if not (
        hasattr(os, "O_NOATIME")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
    ):
        pytest.skip("O_NOATIME directory opens are unavailable")

    probe_dir = tmp_path / "atime-probe"
    probe_dir.mkdir()
    now = time.time_ns()
    old = now - 3 * 3600_000_000_000
    newer = now - 1 * 3600_000_000_000
    os.utime(probe_dir, ns=(old, newer))
    observed_atime = os.stat(probe_dir).st_atime_ns
    os.listdir(probe_dir)
    if os.stat(probe_dir).st_atime_ns == observed_atime:
        pytest.skip("filesystem does not observe directory access-time updates")

    cache = _mkdir(guest, "/root/.bun/install/cache", 0o755)
    pkg = _mkdir(guest, "/root/.bun/install/cache/pkg-old", 0o755)
    _mkdir(guest, "/root/.bun/install/cache/links", 0o755)
    _write(guest, "/root/.bun/install/cache/pkg-old/blob.bin", b"x" * 4096)
    _write(guest, "/usr/local/bin/bun", "#!/bin/sh\nexit 0\n", 0o755)
    os.utime(pkg, ns=(old, newer))
    os.utime(cache, ns=(old, newer))
    runner = FakeRunner(
        {
            "bun pm cache": (0, "/root/.bun/install/cache\n", ""),
            "bun --version": (0, "1.4.4\n", ""),
            "bun pm --help": (0, "bun pm cache prune --max-age <days>\n", ""),
        }
    )

    bun = hio.probe({}, sysroot=str(guest), runner=runner)["cache_paths"]["bun"]

    assert bun["allocated_bytes"] >= 4096
    assert bun["error"] is None
    assert abs(os.stat(cache).st_atime_ns - old) < 1_000_000_000
    assert abs(os.stat(pkg).st_atime_ns - old) < 1_000_000_000


def test_probe_log_access_ready_requires_alloy_default_acl_and_read_bits(
    guest: Path,
) -> None:
    _add_npm(guest)
    _add_acl_tools(guest)
    facts = hio.probe({}, sysroot=str(guest), runner=_acl_runner())
    assert facts["log_access_ready"] is True
    detail = facts["log_access_detail"]
    assert detail["alloy_user"] is True
    assert detail["dirs"]["/data/logs"] == {"traversable": True, "default_acl": True}
    assert all(detail["files"].values())

    without_acl = hio.probe({}, sysroot=str(guest), runner=FakeRunner())
    assert without_acl["log_access_ready"] is False
    assert without_acl["log_access_detail"]["dirs"]["/data/logs"]["default_acl"] is False

    masked = hio.probe(
        {}, sysroot=str(guest),
        runner=_acl_runner(ACL_TEXT.replace("default:mask::r-x", "default:mask::r--")),
    )
    assert masked["log_access_ready"] is False
    assert masked["log_access_detail"]["dirs"]["/data/logs"]["default_acl"] is False

    for name in ("access.log", "error.log", "backend.log"):
        (guest / "data/logs" / name).chmod(0o600)
    root_only = hio.probe({}, sysroot=str(guest), runner=_acl_runner())
    assert root_only["log_access_ready"] is False
    assert root_only["log_access_detail"]["files"]["/data/logs/access.log"] is False

    no_alloy = guest / "etc" / "passwd"
    no_alloy.write_text("root:x:0:0:root:/root:/bin/bash\n")
    assert hio.probe({}, sysroot=str(guest), runner=_acl_runner())["log_access_ready"] is False


def test_probe_policy_hashes_and_journal_bytes(guest: Path) -> None:
    _write(
        guest,
        "/etc/systemd/journald.conf.d/60-fleet-retention.conf",
        "[Journal]\nSystemMaxUse=256M\n",
    )
    _write(
        guest,
        "/etc/systemd/system/alloy.service.d/60-fleet-retention.conf",
        "[Service]\nEnvironment=FLEET_JOURNAL_MAX_AGE=48h\n",
    )
    _mkdir(guest, "/var/log/journal/abc", 0o755)
    _write(guest, "/var/log/journal/abc/system.journal", b"J" * 8192)

    facts = hio.probe({}, sysroot=str(guest), runner=FakeRunner())
    hashes = facts["policy_sha256"]
    assert hashes["journald"] == hashlib.sha256(b"[Journal]\nSystemMaxUse=256M\n").hexdigest()
    assert hashes["alloy_env"] == hashlib.sha256(
        b"[Service]\nEnvironment=FLEET_JOURNAL_MAX_AGE=48h\n"
    ).hexdigest()
    assert hashes["npm_logrotate"] == ""
    assert facts["journal_bytes"] >= 8192


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (b"node\x00index.js\x00", set()),
        (b"/usr/bin/node\x00/opt/app/server.js\x00", set()),
        (b"/usr/bin/apt-get\x00install\x00-y\x00acl\x00", {"apt"}),
        (b"dpkg\x00--configure\x00-a\x00", {"dpkg"}),
        (b"npm\x00run\x00build\x00", {"npm", "build"}),
        (b"yarn\x00cache\x00clean\x00", {"yarn"}),
        (b"make\x00-j4\x00", {"build"}),
        (b"bun\x00/app/server.mjs\x00", set()),
        (b"/usr/local/bin/bun\x00run\x00start\x00", set()),
        (b"bun\x00run\x00app.js\x00", set()),
        (b"bun\x00install\x00", {"bun"}),
        (b"bun\x00install\x00-v\x00", {"bun"}),
        (b"bun\x00install\x00--help\x00", {"bun"}),
        (b"bun\x00add\x00leftpad\x00", {"bun"}),
        (b"bun\x00add\x00leftpad\x00--version\x00", {"bun"}),
        (b"bun\x00run\x00build\x00", {"bun", "build"}),
        (b"bun\x00run\x00build\x00--\x00-v\x00", {"bun", "build"}),
        (b"bun\x00run\x00build\x00--help\x00", {"bun", "build"}),
        (b"bun\x00build\x00", {"bun", "build"}),
        (b"bun\x00pm\x00cache\x00prune\x00", {"bun"}),
        (b"bun\x00pm\x00cache\x00path\x00", set()),
        (b"bun\x00--version\x00", set()),
        (b"bun\x00--revision\x00", set()),
        (b"bun\x00-v\x00", set()),
        (b"bun\x00--help\x00", set()),
        (b"bun\x00-h\x00", set()),
        (b"bun\x00help\x00", set()),
        (b"bunx\x00eslint\x00", {"bun"}),
        (b"bun\x00--smol\x00app.mjs\x00", {"bun"}),
        (b"bun\x00", {"bun"}),
    ],
)
def test_classify_busy_never_blocks_on_a_normal_node_server(argv, expected) -> None:
    assert hio._classify_busy(argv) == expected


def test_probe_busy_tools_detects_held_package_lock_without_argv(guest: Path) -> None:
    _write(guest, "/var/lib/dpkg/lock", "")
    lock = guest / "var/lib/dpkg/lock"
    _add_proc(guest, 7777, ["python3", "-c", "import fcntl"])

    _add_fd(guest, 7777, 3, lock, writable=True)
    facts = hio.probe({}, sysroot=str(guest), runner=FakeRunner())
    assert "dpkg" in facts["busy_tools"]

    _add_fd(guest, 7777, 3, lock, writable=False)
    facts = hio.probe({}, sysroot=str(guest), runner=FakeRunner())
    assert "dpkg" not in facts["busy_tools"]

    # the lock file itself is only ever lstat-ed, never created or removed
    assert lock.exists()
    assert set(hio.REQUIRED_BINARIES) <= set(facts["binaries"])
    assert {"apt-get", "runuser"} <= set(facts["binaries"])


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #


def _capture_spec(root: Path, guest_path: str, *, blob: str, size=None, **over):
    st = (root / guest_path.lstrip("/")).lstat()
    spec = {
        "path": guest_path,
        "blob_path": blob,
        "size": st.st_size if size is None else size,
        "device": st.st_dev,
        "inode": st.st_ino,
        "mtime_ns": st.st_mtime_ns,
        "allocated_bytes": st.st_blocks * 512,
        "compression": "plain",
        "profile": "npm",
        "log_kind": "application",
        "is_active": False,
    }
    spec.update(over)
    return spec


def _blob(spool: Path, index: int) -> str:
    return str(spool / CAPTURE_ID / (f"{index:064x}" + ".blob"))


def _write_released(spool: Path, records: dict) -> Path:
    """Write the importer-owned cumulative release marker (exact wire format)."""
    capture_dir = spool / CAPTURE_ID
    payload = {
        "capture_id": CAPTURE_ID,
        "released": records,
        "released_sha256": hashlib.sha256(
            json.dumps(records, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest(),
    }
    path = capture_dir / hio.RELEASED_FILENAME
    path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    path.chmod(0o600)
    return path


def _fake_capture_blob(spool: Path, index: int, data: bytes, *, total: int):
    """Fabricate a (sparse) blob plus a valid receipt without copying ``total``."""
    capture_dir = spool / CAPTURE_ID
    spool.mkdir(exist_ok=True)
    spool.chmod(0o700)
    namespace = spool / NAMESPACE
    namespace.mkdir(exist_ok=True)
    namespace.chmod(0o700)
    capture_dir.mkdir(exist_ok=True)
    capture_dir.chmod(0o700)
    receipts = capture_dir / hio.RECEIPTS_DIRNAME
    receipts.mkdir(exist_ok=True)
    receipts.chmod(0o700)
    blob = capture_dir / (f"{index:064x}" + ".blob")
    blob.write_bytes(data)
    os.truncate(blob, total)
    blob.chmod(0o600)
    st = blob.lstat()
    digest = "ab" * 32
    entry = {
        "path": "/data/logs/access.log.1",
        "device": st.st_dev,
        "inode": st.st_ino,
        "size": total,
        "mtime_ns": st.st_mtime_ns,
        "allocated_bytes": st.st_blocks * 512,
        "compression": "plain",
        "profile": "npm",
        "log_kind": "application",
        "is_active": False,
        "sha256": digest,
        "captured_bytes": total,
        "captured_ns": 1,
        "blob_path": str(blob),
        "source_path": "/data/logs/access.log.1",
    }
    payload = {"capture_id": CAPTURE_ID, "entry": entry, "blob": hio._blob_meta(st)}
    payload["receipt_sha256"] = hio._receipt_digest(payload)
    receipt_path = receipts / (blob.name + ".json")
    receipt_path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    receipt_path.chmod(0o600)
    return str(blob), digest, entry


def test_capture_freezes_prefix_writes_manifest_and_resumes(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/access.log.1", b"hello world\n")
    spool = tmp_path / "spool"
    blob = _blob(spool, 1)
    request = {
        "capture_id": CAPTURE_ID,
        "files": [_capture_spec(guest, "/data/logs/access.log.1", blob=blob, size=5)],
    }

    facts = hio.capture(request, sysroot=str(guest), spool_root=str(spool))
    assert facts["capture_id"] == CAPTURE_ID
    entry = facts["files"][0]
    assert entry["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert entry["captured_bytes"] == 5
    assert entry["source_path"] == "/data/logs/access.log.1"
    assert Path(blob).read_bytes() == b"hello"
    assert stat.S_IMODE(Path(blob).stat().st_mode) == 0o600
    capture_dir = spool / CAPTURE_ID
    assert stat.S_IMODE(capture_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((spool / NAMESPACE).stat().st_mode) == 0o700
    manifest = json.loads((capture_dir / "manifest.json").read_text())
    assert manifest["manifest_sha256"] == hio._manifest_digest(manifest["files"])
    assert [item["blob_path"] for item in manifest["files"]] == [blob]

    before = Path(blob).lstat()
    again = hio.capture(request, sysroot=str(guest), spool_root=str(spool))
    after = Path(blob).lstat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert again["files"][0]["sha256"] == entry["sha256"]


def test_capture_resume_reuses_validated_blobs_and_recaptures_missing(
    tmp_path, guest: Path
) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/a.log.1", b"aaaa")
    _write(guest, "/data/logs/b.log.1", b"bbbb")
    spool = tmp_path / "spool"
    first = _capture_spec(guest, "/data/logs/a.log.1", blob=_blob(spool, 1))
    second = _capture_spec(guest, "/data/logs/b.log.1", blob=_blob(spool, 2))

    partial = hio.capture(
        {"capture_id": CAPTURE_ID, "files": [first]},
        sysroot=str(guest),
        spool_root=str(spool),
    )
    assert partial["files"][0]["sha256"] == hashlib.sha256(b"aaaa").hexdigest()
    blob_one = Path(_blob(spool, 1))
    inode_before = blob_one.lstat().st_ino

    done = hio.capture(
        {"capture_id": CAPTURE_ID, "files": [first, second]},
        sysroot=str(guest),
        spool_root=str(spool),
    )
    assert [item["sha256"] for item in done["files"]] == [
        hashlib.sha256(b"aaaa").hexdigest(),
        hashlib.sha256(b"bbbb").hexdigest(),
    ]
    assert blob_one.lstat().st_ino == inode_before
    assert Path(_blob(spool, 2)).read_bytes() == b"bbbb"

    # a blob deleted behind the manifest is recaptured, not trusted
    blob_one.unlink()
    recaptured = hio.capture(
        {"capture_id": CAPTURE_ID, "files": [first, second]},
        sysroot=str(guest),
        spool_root=str(spool),
    )
    assert recaptured["files"][0]["sha256"] == hashlib.sha256(b"aaaa").hexdigest()
    assert blob_one.read_bytes() == b"aaaa"


def test_capture_locates_renamed_same_inode(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    original = _write(guest, "/data/logs/access.log.1", b"rotated\n")
    inode = original.lstat().st_ino
    spool = tmp_path / "spool"
    request = {
        "capture_id": CAPTURE_ID,
        "files": [_capture_spec(guest, "/data/logs/access.log.1", blob=_blob(spool, 1))],
    }

    os.rename(guest / "data/logs/access.log.1", guest / "data/logs/access.log.2")
    facts = hio.capture(request, sysroot=str(guest), spool_root=str(spool))
    assert facts["files"][0]["source_path"] == "/data/logs/access.log.2"
    assert facts["files"][0]["inode"] == inode
    assert facts["files"][0]["sha256"] == hashlib.sha256(b"rotated\n").hexdigest()

    # renamed again into an occupied legacy name: the recorded path is gone but
    # the inode is still located under the allowlisted root
    _write(guest, "/data/logs/access.log.3", b"newer native rotation\n")
    os.replace(guest / "data/logs/access.log.2", guest / "data/logs/access.log.3")
    renamed = hio.capture(
        {
            "capture_id": CAPTURE_ID,
            "files": [{**request["files"][0], "blob_path": _blob(spool, 2)}],
        },
        sysroot=str(guest),
        spool_root=str(spool),
    )
    assert renamed["files"][0]["source_path"] == "/data/logs/access.log.3"
    assert renamed["files"][0]["inode"] == inode


def test_capture_truncated_prefix_fails_and_retains_completed(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/a.log.1", b"complete")
    _write(guest, "/data/logs/b.log.1", b"short")
    spool = tmp_path / "spool"
    good = _capture_spec(guest, "/data/logs/a.log.1", blob=_blob(spool, 1))
    bad = _capture_spec(guest, "/data/logs/b.log.1", blob=_blob(spool, 2), size=500)

    with pytest.raises(hio.HelperError):
        hio.capture(
            {"capture_id": CAPTURE_ID, "files": [good, bad]},
            sysroot=str(guest),
            spool_root=str(spool),
        )
    assert Path(_blob(spool, 1)).read_bytes() == b"complete"
    assert not Path(_blob(spool, 2)).exists()
    manifest = json.loads((spool / CAPTURE_ID / "manifest.json").read_text())
    assert [item["path"] for item in manifest["files"]] == ["/data/logs/a.log.1"]


def test_capture_rejects_outside_allowlist_bad_blob_and_symlink(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/etc/secret.log", b"nope")
    spool = tmp_path / "spool"

    outside = _capture_spec(guest, "/etc/secret.log", blob=_blob(spool, 1))
    with pytest.raises(hio.HelperError):
        hio.capture(
            {"capture_id": CAPTURE_ID, "files": [outside]},
            sysroot=str(guest),
            spool_root=str(spool),
        )

    _write(guest, "/data/logs/real.log.1", b"real")
    wrong_blob = _capture_spec(
        guest, "/data/logs/real.log.1", blob=str(spool / "elsewhere" / "x.blob")
    )
    with pytest.raises(hio.HelperError):
        hio.capture(
            {"capture_id": CAPTURE_ID, "files": [wrong_blob]},
            sysroot=str(guest),
            spool_root=str(spool),
        )

    _write(guest, "/data/logs/linked.log.1", b"placeholder")
    (guest / "data/logs/linked.log.1").unlink()
    (guest / "data/logs/linked.log.1").symlink_to("/etc/secret.log")
    st = (guest / "etc/secret.log").lstat()
    symlink = _capture_spec(
        guest,
        "/data/logs/linked.log.1",
        blob=_blob(spool, 3),
        device=st.st_dev,
        inode=st.st_ino,
        size=st.st_size,
    )
    with pytest.raises(hio.HelperError):
        hio.capture(
            {"capture_id": CAPTURE_ID, "files": [symlink]},
            sysroot=str(guest),
            spool_root=str(spool),
        )


def test_capture_requires_two_gibibytes_of_node_reserve(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/huge.log.1", b"small")
    spool = tmp_path / "spool"
    spec = _capture_spec(guest, "/data/logs/huge.log.1", blob=_blob(spool, 1))
    spec["size"] = 1 << 60
    with pytest.raises(hio.HelperError) as excinfo:
        hio.capture(
            {"capture_id": CAPTURE_ID, "files": [spec]},
            sysroot=str(guest),
            spool_root=str(spool),
        )
    assert "space" in str(excinfo.value)


def test_capture_refuses_symlinked_or_permissive_spool_resume(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/a.log.1", b"aaaa")
    spool = tmp_path / "spool"
    request = {
        "capture_id": CAPTURE_ID,
        "files": [_capture_spec(guest, "/data/logs/a.log.1", blob=_blob(spool, 1))],
    }
    hio.capture(request, sysroot=str(guest), spool_root=str(spool))

    namespace_dir = spool / NAMESPACE
    moved = tmp_path / "moved"
    os.rename(namespace_dir, moved)
    namespace_dir.symlink_to(moved)
    with pytest.raises(hio.HelperError):
        hio.capture(request, sysroot=str(guest), spool_root=str(spool))

    namespace_dir.unlink()
    os.rename(moved, namespace_dir)
    (spool / CAPTURE_ID).chmod(0o755)
    with pytest.raises(hio.HelperError):
        hio.capture(request, sysroot=str(guest), spool_root=str(spool))


def test_capture_corrupt_manifest_fails_closed(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/a.log.1", b"aaaa")
    spool = tmp_path / "spool"
    capture_dir = spool / CAPTURE_ID
    capture_dir.mkdir(parents=True)
    spool.chmod(0o700)
    capture_dir.chmod(0o700)
    (capture_dir / "manifest.json").write_text("{not json")
    with pytest.raises(hio.HelperError) as excinfo:
        hio.capture(
            {
                "capture_id": CAPTURE_ID,
                "files": [_capture_spec(guest, "/data/logs/a.log.1", blob=_blob(spool, 1))],
            },
            sysroot=str(guest),
            spool_root=str(spool),
        )
    assert "manifest" in str(excinfo.value)


def test_capture_writes_one_manifest_and_a_receipt_per_blob(
    tmp_path, guest: Path, monkeypatch
) -> None:
    _mkdir(guest, "/data/logs")
    for index in range(3):
        _write(guest, f"/data/logs/a{index}.log.1", f"data-{index}\n".encode())
    spool = tmp_path / "spool"
    specs = [
        _capture_spec(guest, f"/data/logs/a{index}.log.1", blob=_blob(spool, index))
        for index in range(3)
    ]
    calls = []
    real_save = hio._save_manifest

    def counting(capture_dir, capture_id, created_ns, files):
        calls.append(len(list(files)))
        return real_save(capture_dir, capture_id, created_ns, files)

    monkeypatch.setattr(hio, "_save_manifest", counting)
    facts = hio.capture(
        {"capture_id": CAPTURE_ID, "files": specs},
        sysroot=str(guest),
        spool_root=str(spool),
    )

    # exactly one aggregate manifest write for three completed blobs
    assert calls == [3]
    assert [entry["path"] for entry in facts["files"]] == [
        f"/data/logs/a{index}.log.1" for index in range(3)
    ]
    receipts = spool / CAPTURE_ID / hio.RECEIPTS_DIRNAME
    assert stat.S_IMODE(receipts.stat().st_mode) == 0o700
    names = sorted(path.name for path in receipts.glob("*.json"))
    assert names == [f"{index:064x}.blob.json" for index in range(3)]
    for name in names:
        assert stat.S_IMODE((receipts / name).stat().st_mode) == 0o600
    manifest = json.loads((spool / CAPTURE_ID / "manifest.json").read_text())
    assert len(manifest["files"]) == 3
    assert manifest["manifest_sha256"] == hio._manifest_digest(manifest["files"])


def test_capture_resumes_from_receipts_after_manifest_loss(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/a.log.1", b"aaaa")
    _write(guest, "/data/logs/b.log.1", b"bbbb")
    spool = tmp_path / "spool"
    request = {
        "capture_id": CAPTURE_ID,
        "files": [
            _capture_spec(guest, "/data/logs/a.log.1", blob=_blob(spool, 1)),
            _capture_spec(guest, "/data/logs/b.log.1", blob=_blob(spool, 2)),
        ],
    }
    first = hio.capture(request, sysroot=str(guest), spool_root=str(spool))

    # transport crash after the copies but before the aggregate manifest write
    (spool / CAPTURE_ID / "manifest.json").unlink()
    for name in ("a.log.1", "b.log.1"):
        (guest / "data/logs" / name).unlink()  # originals rotate away

    again = hio.capture(request, sysroot=str(guest), spool_root=str(spool))
    assert [entry["sha256"] for entry in again["files"]] == [
        entry["sha256"] for entry in first["files"]
    ]
    assert Path(_blob(spool, 1)).read_bytes() == b"aaaa"
    manifest = json.loads((spool / CAPTURE_ID / "manifest.json").read_text())
    assert len(manifest["files"]) == 2


def test_capture_tampered_blob_is_not_reused_and_is_retained(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/a.log.1", b"aaaa")
    spool = tmp_path / "spool"
    request = {
        "capture_id": CAPTURE_ID,
        "files": [_capture_spec(guest, "/data/logs/a.log.1", blob=_blob(spool, 1))],
    }
    hio.capture(request, sysroot=str(guest), spool_root=str(spool))
    blob = Path(_blob(spool, 1))
    os.utime(blob, ns=(123456789, 987654321))
    (guest / "data/logs/a.log.1").unlink()

    with pytest.raises(hio.HelperError):
        hio.capture(request, sysroot=str(guest), spool_root=str(spool))
    assert blob.read_bytes() == b"aaaa"  # incomplete/orphan bytes are never removed


def test_capture_does_not_recapture_released_blob(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/a.log.1", b"aaaa")
    spool = tmp_path / "spool"
    request = {
        "capture_id": CAPTURE_ID,
        "files": [_capture_spec(guest, "/data/logs/a.log.1", blob=_blob(spool, 1))],
    }
    first = hio.capture(request, sysroot=str(guest), spool_root=str(spool))
    digest = first["files"][0]["sha256"]
    blob = Path(_blob(spool, 1))
    blob.unlink()
    (guest / "data/logs/a.log.1").unlink()

    # without a release marker the missing blob must not be silently accepted
    with pytest.raises(hio.HelperError):
        hio.capture(request, sysroot=str(guest), spool_root=str(spool))

    # a marker naming the wrong digest is never trusted
    _write_released(spool, {blob.name: {"sha256": "0" * 64, "released_ns": 1}})
    with pytest.raises(hio.HelperError):
        hio.capture(request, sysroot=str(guest), spool_root=str(spool))

    # a corrupt marker fails closed
    marker = spool / CAPTURE_ID / hio.RELEASED_FILENAME
    payload = json.loads(marker.read_text())
    payload["released"][blob.name]["sha256"] = digest
    marker.write_text(json.dumps(payload))
    with pytest.raises(hio.HelperError):
        hio.capture(request, sysroot=str(guest), spool_root=str(spool))

    _write_released(spool, {blob.name: {"sha256": digest, "released_ns": 1}})
    again = hio.capture(request, sysroot=str(guest), spool_root=str(spool))
    assert again["files"][0]["sha256"] == digest
    assert not blob.exists()


def test_capture_rejects_inconsistent_provenance(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/a.log.gz", gzip.compress(b"compressed\n"))
    spool = tmp_path / "spool"
    wrong_compression = _capture_spec(
        guest, "/data/logs/a.log.gz", blob=_blob(spool, 1), compression="plain"
    )
    with pytest.raises(hio.HelperError):
        hio.capture(
            {"capture_id": CAPTURE_ID, "files": [wrong_compression]},
            sysroot=str(guest),
            spool_root=str(spool),
        )

    _write(guest, "/var/log/proxmox-backup/tasks/archive", "archive index\n")
    wrong_profile = _capture_spec(
        guest,
        "/var/log/proxmox-backup/tasks/archive",
        blob=_blob(spool, 2),
        profile="npm",
        log_kind="application",
    )
    with pytest.raises(hio.HelperError):
        hio.capture(
            {"capture_id": CAPTURE_ID, "files": [wrong_profile]},
            sysroot=str(guest),
            spool_root=str(spool),
        )


def test_capture_rejects_symlinked_ancestor_directory(tmp_path, guest: Path) -> None:
    real = tmp_path / "real-logs"
    real.mkdir()
    log = real / "a.log.1"
    log.write_bytes(b"aaaa")
    (guest / "data").mkdir(exist_ok=True)
    (guest / "data" / "logs").symlink_to(real)
    st = log.lstat()
    spool = tmp_path / "spool"
    spec = {
        "path": "/data/logs/a.log.1",
        "blob_path": _blob(spool, 1),
        "size": st.st_size,
        "device": st.st_dev,
        "inode": st.st_ino,
        "mtime_ns": st.st_mtime_ns,
        "allocated_bytes": st.st_blocks * 512,
        "compression": "plain",
        "profile": "npm",
        "log_kind": "application",
        "is_active": False,
    }
    with pytest.raises(hio.HelperError):
        hio.capture(
            {"capture_id": CAPTURE_ID, "files": [spec]},
            sysroot=str(guest),
            spool_root=str(spool),
        )
    assert not Path(_blob(spool, 1)).exists()


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #


def _live_spec(root: Path, guest_path: str, *, offset: int, length: int, **over):
    st = (root / guest_path.lstrip("/")).lstat()
    spec = {
        "path": guest_path,
        "offset": offset,
        "length": length,
        "device": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "allocated_bytes": st.st_blocks * 512,
        "compression": "plain",
        "profile": "npm",
        "log_kind": "application",
        "is_active": False,
    }
    spec.update(over)
    return spec


def test_snapshot_streams_ranges_and_manifest(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"0123456789")
    st = log.lstat()
    destination = tmp_path / "snap.tar"
    request = {
        "files": [
            _live_spec(guest, "/data/logs/access.log.1", offset=2, length=3),
            _live_spec(guest, "/data/logs/access.log.1", offset=7, length=3),
        ]
    }

    facts = hio.snapshot(request, destination=str(destination), sysroot=str(guest))
    assert [entry["member"] for entry in facts["files"]] == [
        "ranges/000000.bin",
        "ranges/000001.bin",
    ]
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with tarfile.open(destination) as tar:
        assert tar.getnames() == [
            "manifest.json",
            "ranges/000000.bin",
            "ranges/000001.bin",
        ]
        manifest = json.loads(tar.extractfile("manifest.json").read())
        first = manifest["files"][0]
        assert first["member"] == "ranges/000000.bin"
        assert (first["offset"], first["length"]) == (2, 3)
        assert first["source"] == "live"
        assert first["path"] == "/data/logs/access.log.1"
        assert first["inode"] == st.st_ino
        assert first["log_kind"] == "application"
        assert tar.extractfile("ranges/000000.bin").read() == b"234"
        assert tar.extractfile("ranges/000001.bin").read() == b"789"


def test_snapshot_budget_identity_truncation_and_traversal(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"0123456789")
    st = log.lstat()
    destination = tmp_path / "snap.tar"
    base = _live_spec(guest, "/data/logs/access.log.1", offset=0, length=1)

    over_budget = dict(base, length=(32 * 1024 * 1024) + 1)
    with pytest.raises(hio.HelperError):
        hio.snapshot({"files": [over_budget]}, destination=str(destination), sysroot=str(guest))
    assert not destination.exists()

    wrong_inode = dict(base, inode=st.st_ino + 1)
    with pytest.raises(hio.HelperError):
        hio.snapshot({"files": [wrong_inode]}, destination=str(destination), sysroot=str(guest))

    truncated = dict(base, offset=9, length=5)
    with pytest.raises(hio.HelperError):
        hio.snapshot({"files": [truncated]}, destination=str(destination), sysroot=str(guest))

    outside = dict(base, path="/etc/passwd")
    with pytest.raises(hio.HelperError):
        hio.snapshot({"files": [outside]}, destination=str(destination), sysroot=str(guest))
    assert not destination.exists()


def test_snapshot_rejects_symlink_and_missing_sysroot(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    real = _write(guest, "/data/logs/real.log.1", b"data")
    (guest / "data/logs/link.log.1").symlink_to(real)
    spec = _live_spec(guest, "/data/logs/link.log.1", offset=0, length=2)
    destination = tmp_path / "snap.tar"
    with pytest.raises(hio.HelperError):
        hio.snapshot({"files": [spec]}, destination=str(destination), sysroot=str(guest))
    with pytest.raises(hio.HelperError):
        hio.snapshot({"files": [spec]}, destination=str(destination))
    assert not destination.exists()


def test_snapshot_from_capture_blob_validates_provenance(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/access.log.1", b"0123456789")
    spool = tmp_path / "spool"
    blob = _blob(spool, 1)
    captured = hio.capture(
        {
            "capture_id": CAPTURE_ID,
            "files": [_capture_spec(guest, "/data/logs/access.log.1", blob=blob)],
        },
        sysroot=str(guest),
        spool_root=str(spool),
    )
    digest = captured["files"][0]["sha256"]
    destination = tmp_path / "snap.tar"
    spec = {
        "path": "/data/logs/access.log.1",
        "blob_path": blob,
        "capture_id": CAPTURE_ID,
        "sha256": digest,
        "offset": 4,
        "length": 3,
        "compression": "plain",
        "profile": "npm",
        "log_kind": "application",
        "is_active": False,
    }

    facts = hio.snapshot({"files": [spec]}, destination=str(destination), spool_root=str(spool))
    assert facts["files"][0]["source"] == "capture"
    assert facts["files"][0]["capture_id"] == CAPTURE_ID
    assert facts["files"][0]["blob_path"] == blob
    with tarfile.open(destination) as tar:
        assert tar.extractfile("ranges/000000.bin").read() == b"456"

    with pytest.raises(hio.HelperError):
        hio.snapshot(
            {"files": [dict(spec, sha256="0" * 64)]},
            destination=str(destination),
            spool_root=str(spool),
        )
    with pytest.raises(hio.HelperError):
        hio.snapshot(
            {"files": [dict(spec, sha256=None)]},
            destination=str(destination),
            spool_root=str(spool),
        )


def _blob_spec(blob: str, digest: str, *, offset: int, length: int) -> dict:
    return {
        "path": "/data/logs/access.log.1",
        "blob_path": blob,
        "capture_id": CAPTURE_ID,
        "sha256": digest,
        "offset": offset,
        "length": length,
        "compression": "plain",
        "profile": "npm",
        "log_kind": "application",
        "is_active": False,
    }


def test_snapshot_blob_receipt_validation(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    blob, digest, _entry = _fake_capture_blob(spool, 2, b"0123456789", total=10)
    destination = tmp_path / "snap.tar"
    spec = _blob_spec(blob, digest, offset=0, length=4)

    # a blob whose receipt carries a fabricated digest still works: the helper
    # never re-hashes the whole blob to validate it
    facts = hio.snapshot({"files": [spec]}, destination=str(destination), spool_root=str(spool))
    assert facts["files"][0]["source"] == "capture"
    with tarfile.open(destination) as tar:
        assert tar.extractfile("ranges/000000.bin").read() == b"0123"

    # a blob changed after capture is rejected by the recorded metadata
    os.utime(blob, ns=(111111111, 222222222))
    with pytest.raises(hio.HelperError):
        hio.snapshot({"files": [spec]}, destination=str(destination), spool_root=str(spool))

    receipt = spool / CAPTURE_ID / hio.RECEIPTS_DIRNAME / (Path(blob).name + ".json")
    # a receipt with a valid checksum but a different recorded mtime is rejected
    payload = json.loads(receipt.read_text())
    payload["blob"]["mtime_ns"] = 5
    payload["receipt_sha256"] = hio._receipt_digest(payload)
    receipt.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    with pytest.raises(hio.HelperError):
        hio.snapshot({"files": [spec]}, destination=str(destination), spool_root=str(spool))

    # a corrupted receipt checksum fails closed
    payload["receipt_sha256"] = "0" * 64
    receipt.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    with pytest.raises(hio.HelperError):
        hio.snapshot({"files": [spec]}, destination=str(destination), spool_root=str(spool))

    # a missing receipt is never tolerated for a frozen blob
    receipt.unlink()
    with pytest.raises(hio.HelperError):
        hio.snapshot({"files": [spec]}, destination=str(destination), spool_root=str(spool))
    assert not destination.exists()


def test_snapshot_huge_blob_reads_only_the_requested_range(
    tmp_path: Path, monkeypatch
) -> None:
    spool = tmp_path / "spool"
    payload = b"unique-range-marker\n"
    blob, digest, _entry = _fake_capture_blob(
        spool, 3, payload, total=256 * 1024 * 1024
    )
    real_pread = os.pread
    read: list = []

    def counting_pread(fd, size, offset):
        chunk = real_pread(fd, size, offset)
        read.append(len(chunk))
        return chunk

    monkeypatch.setattr(os, "pread", counting_pread)
    destination = tmp_path / "snap.tar"
    spec = _blob_spec(blob, digest, offset=0, length=len(payload))
    hio.snapshot({"files": [spec]}, destination=str(destination), spool_root=str(spool))

    assert sum(read) == len(payload)
    assert sum(read) < 32 * 1024 * 1024
    with tarfile.open(destination) as tar:
        assert tar.extractfile("ranges/000000.bin").read() == payload


def test_snapshot_rejects_symlinked_receipts_dir(tmp_path, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/access.log.1", b"0123456789")
    spool = tmp_path / "spool"
    blob = _blob(spool, 1)
    captured = hio.capture(
        {
            "capture_id": CAPTURE_ID,
            "files": [_capture_spec(guest, "/data/logs/access.log.1", blob=blob)],
        },
        sysroot=str(guest),
        spool_root=str(spool),
    )
    spec = _blob_spec(blob, captured["files"][0]["sha256"], offset=0, length=3)
    destination = tmp_path / "snap.tar"

    receipts = spool / CAPTURE_ID / hio.RECEIPTS_DIRNAME
    moved = tmp_path / "moved-receipts"
    os.rename(receipts, moved)
    receipts.symlink_to(moved)
    with pytest.raises(hio.HelperError):
        hio.snapshot({"files": [spec]}, destination=str(destination), spool_root=str(spool))
    assert not destination.exists()


# --------------------------------------------------------------------------- #
# prune
# --------------------------------------------------------------------------- #


def _quarantine_for(guest_path: str) -> str:
    root = hio._log_root_for(guest_path)
    relative = guest_path[len(root) + 1:]
    return f"{root}/{hio.QUARANTINE_DIRNAME}/{relative}"


def _prune_spec(root: Path, guest_path: str, *, quarantine=None, **over):
    target = root / guest_path.lstrip("/")
    st = target.lstat()
    spec = {
        "path": guest_path,
        "quarantine_path": quarantine or _quarantine_for(guest_path),
        "device": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "allocated_bytes": st.st_blocks * 512,
        "compression": "plain",
        "profile": "npm",
        "log_kind": "application",
        "is_active": False,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }
    spec.update(over)
    return spec


def test_prune_deletes_closed_archive_through_quarantine(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"old access\n")
    spec = _prune_spec(guest, "/data/logs/access.log.1")

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    entry = facts["files"][0]
    assert entry["state"] == "done"
    assert entry["detail"] == ""
    assert facts["files_pruned"] == 1
    assert facts["bytes_reclaimed"] == entry["bytes_reclaimed"]
    assert facts["bytes_reclaimed"] >= 0
    assert not log.exists()
    quarantine_dir = guest / "data/logs" / hio.QUARANTINE_DIRNAME
    assert stat.S_IMODE(quarantine_dir.stat().st_mode) == 0o700
    assert list(quarantine_dir.iterdir()) == []


def test_prune_creates_missing_nested_quarantine_descendants(guest: Path) -> None:
    """A closed PBS UPID under its hex fanout is deleted even though the nested
    quarantine parent (which preserves the source-relative directory) is absent.
    """
    _add_pbs(guest)
    path = f"/var/log/proxmox-backup/tasks/CD/{PBS_UPID_IDLE}"
    log = guest / path.lstrip("/")
    spec = _prune_spec(guest, path, profile="pbs", log_kind="task")
    quarantine = _quarantine_for(path)
    nested_parent = (guest / quarantine.lstrip("/")).parent
    assert not nested_parent.exists()

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    entry = facts["files"][0]
    assert entry["state"] == "done"
    assert entry["detail"] == ""
    assert facts["files_pruned"] == 1
    assert not log.exists()
    quarantine_root = guest / "var/log/proxmox-backup/tasks" / hio.QUARANTINE_DIRNAME
    assert stat.S_IMODE(quarantine_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(nested_parent.stat().st_mode) == 0o700
    assert not any(p.is_file() for p in quarantine_root.rglob("*"))


def test_prune_creates_nested_quarantine_parent_for_npm_archive(guest: Path) -> None:
    """NPM rotated archives in a subdirectory get a nested quarantine parent."""
    _mkdir(guest, "/data/logs/backend")
    log = _write(guest, "/data/logs/backend/access.log.1", b"old backend\n")
    spec = _prune_spec(guest, "/data/logs/backend/access.log.1")
    nested_parent = (guest / _quarantine_for(spec["path"]).lstrip("/")).parent
    assert not nested_parent.exists()

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    assert facts["files"][0]["state"] == "done"
    assert not log.exists()
    assert stat.S_IMODE(nested_parent.stat().st_mode) == 0o700
    assert not any(p.is_file() for p in nested_parent.rglob("*"))


def test_prune_refuses_symlinked_nested_quarantine_ancestor(guest: Path) -> None:
    """A symlink standing in for a required quarantine descendant is refused."""
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"keep me\n")
    _mkdir(guest, "/data/logs/" + hio.QUARANTINE_DIRNAME, 0o700)
    outside = _mkdir(guest, "/data/outside", 0o700)
    (guest / "data/logs" / hio.QUARANTINE_DIRNAME / "sub").symlink_to(outside)
    spec = _prune_spec(
        guest,
        "/data/logs/access.log.1",
        quarantine=f"/data/logs/{hio.QUARANTINE_DIRNAME}/sub/access.log.1",
    )

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["state"] == "blocked"

    assert log.exists()
    assert list(outside.iterdir()) == []


def test_prune_protects_current_control_and_active_files(guest: Path) -> None:
    _add_npm(guest)
    _add_pbs(guest)
    cases = [
        ("/data/logs/access.log", "npm", "application"),
        ("/var/log/proxmox-backup/api/access.log", "pbs", "api"),
        ("/var/log/proxmox-backup/tasks/archive", "pbs", "task_index"),
        (f"/var/log/proxmox-backup/tasks/AB/{PBS_UPID_ACTIVE}", "pbs", "task"),
    ]
    specs = [
        _prune_spec(guest, path, profile=profile, log_kind=kind)
        for path, profile, kind in cases
    ]

    facts = hio.prune({"files": specs}, sysroot=str(guest))

    assert all(entry["state"] == "conflict" for entry in facts["files"])
    assert facts["files_pruned"] == 0
    assert all((guest / path.lstrip("/")).exists() for path, _p, _k in cases)
    details = " | ".join(entry["detail"] for entry in facts["files"])
    assert "active PBS task" in details


def test_prune_requires_digest_identity_and_allowlisted_paths(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _write(guest, "/data/logs/access.log.1", b"x")
    spec = _prune_spec(guest, "/data/logs/access.log.1")

    with pytest.raises(hio.HelperError):
        hio.prune({"files": [dict(spec, sha256="")]}, sysroot=str(guest))
    missing = dict(spec)
    missing.pop("mtime_ns")
    with pytest.raises(hio.HelperError):
        hio.prune({"files": [missing]}, sysroot=str(guest))
    with pytest.raises(hio.HelperError):
        hio.prune({"files": [dict(spec, path="/etc/passwd")]}, sysroot=str(guest))
    with pytest.raises(hio.HelperError):
        hio.prune({"files": [dict(spec, path="/data/logs/../etc/passwd")]}, sysroot=str(guest))
    with pytest.raises(hio.HelperError):
        hio.prune(
            {"files": [dict(spec, quarantine_path="/data/logs/elsewhere/access.log.1")]},
            sysroot=str(guest),
        )


def test_prune_preserves_open_writer_and_allows_read_only_reader(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"being written\n")
    _add_fd(guest, 4242, 5, log, writable=True)
    spec = _prune_spec(guest, "/data/logs/access.log.1")

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["state"] == "conflict"
    assert "open writable fd" in facts["files"][0]["detail"]
    assert log.exists()

    for entry in (guest / "proc/4242/fd").iterdir():
        entry.unlink()
    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["state"] == "done"
    assert not log.exists()


def test_prune_detects_identity_and_digest_changes(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"abcdef")
    spec = _prune_spec(guest, "/data/logs/access.log.1")

    original = log.lstat()
    os.utime(log, ns=(original.st_atime_ns, original.st_mtime_ns + 10_000_000))
    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["state"] == "conflict"
    assert "mtime changed" in facts["files"][0]["detail"]

    log.write_bytes(b"ABCDEF")
    os.utime(log, ns=(original.st_atime_ns, original.st_mtime_ns))
    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["state"] == "conflict"
    assert facts["files"][0]["detail"] == "digest changed"
    assert log.exists()


def test_prune_rejects_symlinked_ancestor_directory(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    _mkdir(guest, "/data/logs/real", 0o755)
    log = _write(guest, "/data/logs/real/evil.log.1", b"x")
    (guest / "data/logs/sub").symlink_to("real")
    spec = _prune_spec(
        guest,
        "/data/logs/sub/evil.log.1",
        quarantine="/data/logs/.fleet-housekeeping-quarantine/evil.log.1",
    )
    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["state"] == "blocked"
    assert log.exists()


def test_prune_recovers_interrupted_quarantine(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    quarantine_dir = _mkdir(guest, "/data/logs/" + hio.QUARANTINE_DIRNAME, 0o700)
    quarantined = quarantine_dir / "access.log.1"
    quarantined.write_bytes(b"old access\n")
    st = quarantined.lstat()
    spec = {
        "path": "/data/logs/access.log.1",
        "quarantine_path": f"/data/logs/{hio.QUARANTINE_DIRNAME}/access.log.1",
        "device": st.st_dev,
        "inode": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "compression": "plain",
        "profile": "npm",
        "log_kind": "application",
        "is_active": False,
        "sha256": hashlib.sha256(b"old access\n").hexdigest(),
    }

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["state"] == "done"
    assert facts["files_pruned"] == 1
    assert not quarantined.exists()
    assert not (guest / "data/logs/access.log.1").exists()

    # a different file at the original path is never overwritten
    quarantined.write_bytes(b"old access\n")
    st = quarantined.lstat()
    spec.update(
        {"device": st.st_dev, "inode": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    )
    occupant = _write(guest, "/data/logs/access.log.1", b"new live log\n")
    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["state"] == "conflict"
    assert "occupied" in facts["files"][0]["detail"]
    assert quarantined.exists()
    assert occupant.read_bytes() == b"new live log\n"

    # an identity change in the quarantined file is retained, never deleted
    occupant.unlink()
    quarantined.write_bytes(b"tampered\n")
    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["state"] == "conflict"
    assert quarantined.exists()


def test_prune_restores_when_a_writer_appears_during_quarantine(
    monkeypatch, guest: Path
) -> None:
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"racy\n")
    spec = _prune_spec(guest, "/data/logs/access.log.1")
    wanted = (log.lstat().st_dev, log.lstat().st_ino)
    calls = {"count": 0}

    def fake_scan(proc_root):
        calls["count"] += 1
        return (set() if calls["count"] == 1 else {wanted}), []

    monkeypatch.setattr(hio, "_scan_proc", fake_scan)
    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    assert facts["files"][0]["state"] == "restored"
    assert "writable fd" in facts["files"][0]["detail"]
    assert log.read_bytes() == b"racy\n"
    assert list((guest / "data/logs" / hio.QUARANTINE_DIRNAME).iterdir()) == []


def test_prune_never_overwrites_an_occupied_quarantine_target(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"target\n")
    _mkdir(guest, "/data/logs/" + hio.QUARANTINE_DIRNAME, 0o700)
    other = _write(
        guest, f"/data/logs/{hio.QUARANTINE_DIRNAME}/access.log.1", b"someone else\n"
    )
    spec = _prune_spec(guest, "/data/logs/access.log.1")

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["state"] == "conflict"
    assert "different file" in facts["files"][0]["detail"]
    assert log.exists()
    assert other.read_bytes() == b"someone else\n"


def test_prune_missing_log_root_and_missing_file(guest: Path) -> None:
    spec = {
        "path": "/data/logs/gone.log.1",
        "quarantine_path": "/data/logs/.fleet-housekeeping-quarantine/gone.log.1",
        "device": 1,
        "inode": 2,
        "size": 3,
        "mtime_ns": 4,
        "compression": "plain",
        "profile": "npm",
        "log_kind": "application",
        "is_active": False,
        "sha256": "ab" * 32,
    }
    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["state"] == "conflict"
    assert facts["files"][0]["detail"] == "log root is missing"

    _mkdir(guest, "/data/logs")
    facts = hio.prune({"files": [spec]}, sysroot=str(guest))
    assert facts["files"][0]["detail"] == "file is missing"
    assert facts["files_pruned"] == 0


def _absent_spec(guest_path: str, **over):
    """A wire record for a path that may not exist (recovery re-observation)."""
    spec = {
        "path": guest_path,
        "quarantine_path": _quarantine_for(guest_path),
        "device": 11,
        "inode": 22,
        "size": 33,
        "mtime_ns": 44,
        "compression": "plain",
        "profile": "npm",
        "log_kind": "application",
        "is_active": False,
        "sha256": "cd" * 32,
    }
    spec.update(over)
    return spec


@pytest.mark.parametrize("path", ["/data/logs/access.log.1", "/data/logs/removed-parent/access.log.1"])
def test_prune_recovery_both_paths_absent_resolves_without_deletion(guest: Path, path: str) -> None:
    _mkdir(guest, "/data/logs")
    spec = _absent_spec(path, recovery=True)

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    entry = facts["files"][0]
    assert entry["state"] == "absent"
    assert entry["bytes_reclaimed"] == 0
    assert facts["files_pruned"] == 0
    assert facts["bytes_reclaimed"] == 0
    # Resolving an absent recovery source never fabricates a quarantine tree.
    assert not (guest / "data/logs" / hio.QUARANTINE_DIRNAME).exists()


def test_prune_fresh_missing_is_a_conflict_not_absent(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    spec = _absent_spec("/data/logs/access.log.1")

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    assert facts["files"][0]["state"] == "conflict"


def test_prune_recovery_requires_both_paths_positively_absent(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    # An unrelated file occupying the quarantine path is not positive absence.
    _mkdir(guest, "/data/logs/" + hio.QUARANTINE_DIRNAME, 0o700)
    other = _write(
        guest, f"/data/logs/{hio.QUARANTINE_DIRNAME}/access.log.1", b"someone else\n"
    )
    spec = _absent_spec("/data/logs/access.log.1", recovery=True)

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    assert facts["files"][0]["state"] == "conflict"
    assert other.read_bytes() == b"someone else\n"


def test_prune_recovery_symlinked_quarantine_is_not_absent(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    outside = _mkdir(guest, "/data/outside", 0o755)
    (guest / "data/logs" / hio.QUARANTINE_DIRNAME).symlink_to(outside)
    spec = _absent_spec("/data/logs/access.log.1", recovery=True)

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    assert facts["files"][0]["state"] == "blocked"
    assert list(outside.iterdir()) == []


def test_prune_recovery_occupied_original_is_a_conflict(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"a replacement\n")
    spec = _absent_spec("/data/logs/access.log.1", recovery=True)

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    assert facts["files"][0]["state"] == "conflict"
    assert "device/inode changed" in facts["files"][0]["detail"]
    assert log.exists()
    assert not (guest / "data/logs" / hio.QUARANTINE_DIRNAME).exists()


def test_prune_recovery_prunes_a_still_present_matching_source(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"still here\n")
    spec = _prune_spec(guest, "/data/logs/access.log.1", recovery=True)

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    # The rename never happened: the recorded identity is intact and prunable.
    assert facts["files"][0]["state"] == "done"
    assert facts["files_pruned"] == 1
    assert not log.exists()


def test_prune_recovery_completes_a_matching_quarantined_file(guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    quarantine_dir = _mkdir(guest, "/data/logs/" + hio.QUARANTINE_DIRNAME, 0o700)
    quarantined = quarantine_dir / "access.log.1"
    quarantined.write_bytes(b"old access\n")
    st = quarantined.lstat()
    spec = _absent_spec(
        "/data/logs/access.log.1",
        recovery=True,
        device=st.st_dev,
        inode=st.st_ino,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        sha256=hashlib.sha256(b"old access\n").hexdigest(),
    )

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    assert facts["files"][0]["state"] == "done"
    assert facts["files_pruned"] == 1
    assert not quarantined.exists()


@pytest.mark.parametrize("fault_at", ["next_file", "quarantine_recheck"])
def test_prune_blocked_stops_batch_and_keeps_done_and_pending(
    monkeypatch, guest: Path, fault_at: str,
) -> None:
    _mkdir(guest, "/data/logs")
    first = _write(guest, "/data/logs/access.log.1", b"one\n")
    second = _write(guest, "/data/logs/access.log.2", b"two\n")
    third = _write(guest, "/data/logs/access.log.3", b"three\n")
    specs = [
        _prune_spec(guest, "/data/logs/access.log.1"),
        _prune_spec(guest, "/data/logs/access.log.2"),
        _prune_spec(guest, "/data/logs/access.log.3"),
    ]
    real_scan = hio._scan_proc

    def fake_scan(proc_root):
        first_done = not first.exists() and not (guest / _quarantine_for(specs[0]["path"]).lstrip("/")).exists()
        if first_done and (fault_at == "next_file" or not second.exists()):
            raise hio.HelperError("uninspectable process descriptors")
        return real_scan(proc_root)

    monkeypatch.setattr(hio, "_scan_proc", fake_scan)

    facts = hio.prune({"files": specs}, sysroot=str(guest))

    assert [entry["state"] for entry in facts["files"]] == ["done", "blocked", "pending"]
    assert facts["files_pruned"] == 1
    assert not first.exists()
    assert second.read_bytes() == b"two\n"
    assert third.read_bytes() == b"three\n"


def test_prune_blocks_when_a_live_writer_fd_is_uninspectable(
    monkeypatch, guest: Path
) -> None:
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"live writer\n")
    _add_denied_proc(guest, 7777, state="R")
    _deny_fd_enumeration(monkeypatch, [7777])
    spec = _prune_spec(guest, "/data/logs/access.log.1")

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    assert facts["files"][0]["state"] == "blocked"
    assert log.read_bytes() == b"live writer\n"
    assert facts["files_pruned"] == 0
    assert not (guest / "data/logs" / hio.QUARANTINE_DIRNAME).exists()


def test_prune_foreign_owned_quarantine_is_blocked(monkeypatch, guest: Path) -> None:
    _mkdir(guest, "/data/logs")
    log = _write(guest, "/data/logs/access.log.1", b"keep me\n")
    spec = _prune_spec(guest, "/data/logs/access.log.1")
    real_fstat = os.fstat

    def foreign_owner(fd):
        observed = real_fstat(fd)
        if stat.S_ISDIR(observed.st_mode) and os.readlink(f"/proc/self/fd/{fd}").endswith(hio.QUARANTINE_DIRNAME):
            fields = list(observed)
            fields[4] = os.geteuid() + 4242
            return os.stat_result(fields)
        return observed

    monkeypatch.setattr(hio.os, "fstat", foreign_owner)

    facts = hio.prune({"files": [spec]}, sysroot=str(guest))

    assert facts["files"][0]["state"] == "blocked"
    assert log.read_bytes() == b"keep me\n"
    assert facts["files_pruned"] == 0




# --------------------------------------------------------------------------- #
# CLI and packaging
# --------------------------------------------------------------------------- #


def _run_cli(args, cwd=None, env=None):
    return subprocess.run(
        [sys.executable, str(MODULE), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


def test_cli_probe_emits_one_compact_json_object(tmp_path, guest: Path) -> None:
    _add_npm(guest)
    _add_acl_tools(guest)
    request = tmp_path / "request.json"
    request.write_text("{}")

    proc = _run_cli(
        [
            "--operation",
            "probe",
            "--lxc-id",
            "123",
            "--request",
            str(request),
            "--guest-local",
            "--sysroot",
            str(guest),
        ]
    )

    assert proc.returncode == 0
    assert proc.stdout.count("\n") == 1
    payload = json.loads(proc.stdout)
    assert payload["guest"]["name"] == "npm-ct"
    assert payload["profiles"] == ["npm"]
    assert set(payload["policy_sha256"]) == {"journald", "alloy_env", "npm_logrotate"}
    assert payload["logging"] == {"plugins": {}, "sources": {}}


def test_cli_failures_stay_json_and_nonzero(tmp_path, guest: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": "/etc/passwd",
                        "quarantine_path": "/etc/passwd",
                        "device": 0,
                        "inode": 0,
                        "size": 0,
                        "mtime_ns": 0,
                        "compression": "plain",
                        "profile": "npm",
                        "log_kind": "application",
                        "is_active": False,
                        "sha256": "ab" * 32,
                    }
                ]
            }
        )
    )
    proc = _run_cli(
        [
            "--operation",
            "prune",
            "--lxc-id",
            "123",
            "--request",
            str(request),
            "--guest-local",
            "--sysroot",
            str(guest),
        ]
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["error"]

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    proc = _run_cli(
        ["--operation", "probe", "--lxc-id", "1", "--request", str(bad), "--guest-local"]
    )
    assert proc.returncode == 1
    assert "error" in json.loads(proc.stdout)


def test_cli_requires_a_request_source() -> None:
    proc = _run_cli(["--operation", "probe", "--lxc-id", "1"])
    assert proc.returncode != 0


def test_cli_runs_standalone_from_an_unrelated_directory(tmp_path, guest: Path) -> None:
    """The helper must stay stdlib-only: no proxmox_fleet import is needed."""
    _add_npm(guest)
    request = tmp_path / "request.json"
    request.write_text("{}")
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    proc = _run_cli(
        [
            "--operation",
            "probe",
            "--lxc-id",
            "123",
            "--request",
            str(request),
            "--guest-local",
            "--sysroot",
            str(guest),
        ],
        cwd=str(tmp_path),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["profiles"] == ["npm"]



def test_capture_uses_pinned_mount_root_but_rejects_guest_symlink_descendants(tmp_path, guest, monkeypatch):
    _write(guest, "/data/logs/access.log.1", b"private mount history\n")
    spool = tmp_path / "spool"
    spec = _capture_spec(guest, "/data/logs/access.log.1", blob=_blob(spool, 1))
    descriptor = os.open(guest, os.O_RDONLY | os.O_DIRECTORY)
    anchor = f"/proc/self/fd/{descriptor}"
    monkeypatch.setattr(hio, "_PINNED_GUEST_ROOTS", {anchor: descriptor})
    try:
        captured = hio.capture({"capture_id": CAPTURE_ID, "files": [spec]},
                               sysroot=anchor, spool_root=str(spool))
        assert Path(spec["blob_path"]).read_bytes() == b"private mount history\n"
        assert captured["files"][0]["sha256"] == hashlib.sha256(b"private mount history\n").hexdigest()
        real = guest / "data/logs"
        moved = guest / "data/real-logs"
        real.rename(moved)
        real.symlink_to(moved)
        second = dict(spec, blob_path=_blob(spool, 2))
        with pytest.raises(hio.HelperError):
            hio.capture({"capture_id": CAPTURE_ID, "files": [second]},
                        sysroot=anchor, spool_root=str(spool))
        assert not Path(second["blob_path"]).exists()
    finally:
        os.close(descriptor)


def test_guest_request_larger_than_kernel_argument_limit_round_trips(tmp_path, guest):
    pct = tmp_path / "pct"
    pct.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "args = sys.argv[sys.argv.index('--') + 1:]\n"
        f"os.execvp(args[0], args + ['--sysroot', {str(guest)!r}])\n"
    )
    pct.chmod(0o755)
    request = {"guest": {"name": "fixture", "os_type": "debian", "is_running": False,
                         "is_template": False}, "padding": "x" * 300000}
    result = hio._pct_guest_call(str(pct), "probe", "123", request)
    assert result["guest"]["name"] == "fixture"
    assert result["guest"]["is_running"] is False
    assert result["files"] == []


def test_managed_policy_hash_overrides_native_policy_without_inferring_success(guest):
    native = _write(guest, "/etc/logrotate.d/nginx-proxy-manager", "native policy")
    assert hio._policy_hashes(str(guest))["npm_logrotate"] == hashlib.sha256(native.read_bytes()).hexdigest()
    managed = _write(guest, hio.POLICY_FILES["npm_logrotate"], "managed policy")
    assert hio._policy_hashes(str(guest))["npm_logrotate"] == hashlib.sha256(managed.read_bytes()).hexdigest()
    managed.unlink()
    assert hio._policy_hashes(str(guest))["npm_logrotate"] == hashlib.sha256(native.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# catalogue logging evidence
# --------------------------------------------------------------------------- #

_RUNNING = {"name": "guest", "os_type": "debian", "is_running": True, "is_template": False}
_STOPPED = {"name": "guest", "os_type": "debian", "is_running": False, "is_template": False}


def _unit_payload(name, *, scope="system", user=None):
    key = f"user:{user}:{name}" if scope == "user" else f"system:{name}"
    return {"name": name, "scope": scope, "user": user, "key": key}


def _layout_request(layout_id="default", *, required=(), running_any=(), source_ids=()):
    return {
        "id": layout_id,
        "required_units": list(required),
        "running_units_any": list(running_any),
        "source_ids": list(source_ids),
    }


def _service_request(*layouts, journal_units=(), includes=None):
    return {
        "layouts": list(layouts),
        "journal_units": list(journal_units),
        "includes": dict(includes or {}),
    }


def _descriptor(*, roots=(), paths=(), exact_files=(), exclude_paths=(), required=True):
    return {
        "roots": list(roots),
        "paths": list(paths),
        "exact_files": list(exact_files),
        "exclude_paths": list(exclude_paths),
        "required": required,
    }


def _logging_request(plugins, sources):
    return {"plugins": plugins, "sources": sources}


def _systemd_runner(states=None, *, acl=ACL_TEXT, extra=None, default=None):
    mapping = {}
    for name, state in (states or {}).items():
        if state is None:
            continue
        load = "not-found" if state == "missing" else "loaded"
        active = "active" if state == "active" else "inactive"
        mapping[
            "systemctl show --no-pager --property=LoadState "
            f"--property=ActiveState -- {name}"
        ] = (0, f"LoadState={load}\nActiveState={active}\n", "")
    if acl is not None:
        mapping["getfacl -p "] = (0, acl, "")
    if extra:
        mapping.update(extra)
    return FakeRunner(mapping, default=default)


def _probe_logging(guest, request, runner, *, running=True):
    return hio.probe(
        {"guest": dict(_RUNNING if running else _STOPPED), "logging": request},
        sysroot=str(guest),
        runner=runner,
    )


def _demo_request(**overrides):
    layouts = {
        "default": overrides.pop("descriptor", None)
        or _descriptor(
            roots=["/var/log/demoapp"],
            paths=["/var/log/demoapp/*.log"],
            exclude_paths=["/var/log/demoapp/*.log.*"],
        )
    }
    return _logging_request(
        {
            "demoapp": _service_request(
                _layout_request(required=[_unit_payload("demoapp.service")])
            )
        },
        {"demoapp/application": {"layouts": layouts}},
    )


def test_probe_logging_reports_quiet_active_and_exact_file_evidence(guest: Path) -> None:
    _mkdir(guest, "/var/log/demoapp", 0o755)
    _mkdir(guest, "/var/log/demoapp/nested", 0o755)
    _write(guest, "/var/log/demoapp/live.log", "live\n")
    _write(guest, "/var/log/demoapp/live.log.1", "old\n")
    _write(guest, "/var/log/demoapp/nested/nested.log", "nested\n")
    _mkdir(guest, "/opt/demoapp/data", 0o755)
    _write(guest, "/opt/demoapp/data/error.log", "error\n")
    _write(guest, "/opt/demoapp/data/demo.db", "secret\n", 0o600)
    _add_acl_tools(guest)
    request = _logging_request(
        {
            "demoapp": _service_request(
                _layout_request(
                    required=[
                        _unit_payload("demoapp.service"),
                        _unit_payload("optional.timer"),
                    ]
                ),
                journal_units=[_unit_payload("journal-only.service")],
            )
        },
        {
            "demoapp/application": {
                "layouts": {
                    "default": _descriptor(
                        roots=["/var/log/demoapp"],
                        paths=["/var/log/demoapp/*.log"],
                        exclude_paths=["/var/log/demoapp/*.log.*"],
                    )
                }
            },
            "demoapp/error": {
                "layouts": {
                    "default": _descriptor(
                        exact_files=["/opt/demoapp/data/error.log"]
                    )
                }
            },
            "demoapp/empty": {
                "layouts": {
                    "default": _descriptor(
                        roots=["/var/log/demoapp"],
                        paths=["/var/log/demoapp/*.audit"],
                    )
                }
            },
        },
    )
    facts = _probe_logging(
        guest,
        request,
        _systemd_runner(
            {
                "demoapp.service": "active",
                "optional.timer": "inactive",
                "journal-only.service": "active",
            }
        ),
    )
    logging = facts["logging"]
    units = logging["plugins"]["demoapp"]["units"]
    assert units["system:demoapp.service"]["status"] == "active"
    assert units["system:optional.timer"]["status"] == "inactive"
    assert units["system:journal-only.service"]["status"] == "active"
    app = logging["sources"]["demoapp/application"]["layouts"]["default"]
    assert app["status"] == "ok"
    assert app["roots"]["/var/log/demoapp"]["access"] == {
        "traversable": True,
        "readable": True,
        "default_acl": True,
    }
    assert app["matches"][0]["matched"] == 1
    assert [entry["path"] for entry in app["matches"][0]["files"]] == [
        "/var/log/demoapp/live.log"
    ]
    assert app["matches"][0]["files"][0]["accessible"] is True
    exact = logging["sources"]["demoapp/error"]["layouts"]["default"]
    assert exact["status"] == "ok"
    assert exact["exact_files"]["/opt/demoapp/data/error.log"]["access"] == {
        "readable": True
    }
    assert "demo.db" not in json.dumps(logging)
    empty = logging["sources"]["demoapp/empty"]["layouts"]["default"]
    assert empty["status"] == "ok"
    assert empty["matches"][0]["status"] == "quiet"
    assert empty["matches"][0]["matched"] == 0
    _validate_logging_facts(logging, request)

    # A request-less probe and a stopped guest both return explicit empty maps;
    # neither silently omits the key.
    assert hio.probe({}, sysroot=str(guest), runner=_systemd_runner())["logging"] == {
        "plugins": {},
        "sources": {},
    }
    assert _probe_logging(
        guest, request, _systemd_runner(), running=False
    )["logging"] == {"plugins": {}, "sources": {}}
    assert hio._stopped_probe_facts(dict(_STOPPED))["logging"] == {
        "plugins": {},
        "sources": {},
    }


def test_probe_logging_requires_alloy_traverse_and_default_acl(guest: Path) -> None:
    _mkdir(guest, "/root/.local/share/demoapp/logs", 0o755)
    _mkdir(guest, "/root/.local", 0o755)
    _mkdir(guest, "/root/.local/share", 0o755)
    _mkdir(guest, "/root/.local/share/demoapp", 0o755)
    _write(guest, "/root/.local/share/demoapp/logs/a.log", "a\n")
    _add_acl_tools(guest)
    (guest / "root").chmod(0o700)
    request = _demo_request(
        descriptor=_descriptor(
            roots=["/root/.local/share/demoapp/logs"],
            paths=["/root/.local/share/demoapp/logs/*.log"],
        )
    )
    denied = _probe_logging(
        guest, request, _systemd_runner({"demoapp.service": "active"})
    )
    denied_source = denied["logging"]["sources"]["demoapp/application"]["layouts"][
        "default"
    ]
    assert denied_source["roots"]["/root/.local/share/demoapp/logs"]["status"] == "denied"
    assert (
        denied_source["roots"]["/root/.local/share/demoapp/logs"]["access"][
            "traversable"
        ]
        is False
    )
    assert denied_source["status"] == "denied"

    (guest / "root").chmod(0o711)
    allowed = _probe_logging(
        guest, request, _systemd_runner({"demoapp.service": "active"})
    )
    allowed_source = allowed["logging"]["sources"]["demoapp/application"]["layouts"][
        "default"
    ]
    assert allowed_source["status"] == "ok"
    assert allowed_source["matches"][0]["status"] == "ok"

    masked = _probe_logging(
        guest,
        request,
        _systemd_runner(
            {"demoapp.service": "active"},
            acl=ACL_TEXT.replace("default:mask::r-x", "default:mask::r--"),
        ),
    )
    masked_source = masked["logging"]["sources"]["demoapp/application"]["layouts"][
        "default"
    ]
    assert masked_source["status"] == "denied"
    assert (
        masked_source["roots"]["/root/.local/share/demoapp/logs"]["access"][
            "default_acl"
        ]
        is False
    )

    (guest / "usr/bin/getfacl").unlink()
    unmeasured = _probe_logging(
        guest, request, _systemd_runner({"demoapp.service": "active"})
    )
    unmeasured_source = unmeasured["logging"]["sources"]["demoapp/application"][
        "layouts"
    ]["default"]
    assert unmeasured_source["status"] == "unavailable"
    assert "access" not in unmeasured_source["roots"][
        "/root/.local/share/demoapp/logs"
    ]

    _write(guest, "/etc/passwd", "root:x:0:0:root:/root:/bin/bash\n")
    no_user = _probe_logging(
        guest, request, _systemd_runner({"demoapp.service": "active"})
    )
    assert (
        no_user["logging"]["sources"]["demoapp/application"]["layouts"]["default"][
            "status"
        ]
        == "unavailable"
    )


def test_probe_logging_exact_file_does_not_enumerate_app_data(guest: Path) -> None:
    _mkdir(guest, "/opt/uptime-kuma/data", 0o755)
    _write(guest, "/opt/uptime-kuma/data/error.log", "error\n")
    _write(guest, "/opt/uptime-kuma/data/kuma.db", "database\n", 0o600)
    _add_acl_tools(guest)
    request = _logging_request(
        {
            "uptimekuma": _service_request(
                _layout_request(required=[_unit_payload("uptime-kuma.service")])
            )
        },
        {
            "uptimekuma/error": {
                "layouts": {
                    "default": _descriptor(
                        exact_files=["/opt/uptime-kuma/data/error.log"]
                    )
                }
            }
        },
    )
    facts = _probe_logging(
        guest, request, _systemd_runner({"uptime-kuma.service": "active"})
    )
    source = facts["logging"]["sources"]["uptimekuma/error"]["layouts"]["default"]
    assert source["roots"] == {}
    assert source["status"] == "ok"
    assert source["matches"] == []
    assert "kuma.db" not in json.dumps(facts["logging"])

    (guest / "opt/uptime-kuma/data/error.log").chmod(0o600)
    denied = _probe_logging(
        guest, request, _systemd_runner({"uptime-kuma.service": "active"})
    )
    denied_source = denied["logging"]["sources"]["uptimekuma/error"]["layouts"][
        "default"
    ]
    assert denied_source["status"] == "denied"
    assert denied_source["exact_files"]["/opt/uptime-kuma/data/error.log"][
        "access"
    ] == {"readable": False}
    assert "kuma.db" not in json.dumps(denied["logging"])


def test_probe_logging_matching_symlinks_fail_the_source(guest: Path) -> None:
    _mkdir(guest, "/var/log/demoapp/nested", 0o755)
    _write(guest, "/var/log/demoapp/current.log", "current\n")
    _write(guest, "/var/log/demoapp/current.log.1", "old\n")
    _write(guest, "/var/log/demoapp/nested/deep.log", "deep\n")
    _mkdir(guest, "/srv/outside", 0o755)
    _write(guest, "/srv/outside/secret.log", "secret\n")
    (guest / "var/log/demoapp/escape").symlink_to(guest / "srv/outside")
    (guest / "var/log/demoapp/link.log").symlink_to(guest / "srv/outside/secret.log")
    _add_acl_tools(guest)
    request = _demo_request(
        descriptor=_descriptor(
            roots=["/var/log/demoapp"],
            paths=["/var/log/demoapp/**/*.log"],
            exclude_paths=["/var/log/demoapp/*.log.1"],
        )
    )
    facts = _probe_logging(
        guest, request, _systemd_runner({"demoapp.service": "active"})
    )
    source = facts["logging"]["sources"]["demoapp/application"]["layouts"]["default"]
    assert source["status"] == "failed"
    assert source["roots"]["/var/log/demoapp"]["status"] == "failed"
    assert source["matches"][0]["status"] == "failed"
    assert "secret.log" not in json.dumps(facts["logging"])


def test_probe_logging_unmatched_or_excluded_symlink_aliases_are_ignored(
    guest: Path,
) -> None:
    _mkdir(guest, "/var/log/seerr", 0o755)
    _write(guest, "/var/log/seerr/seerr-2026-10-26.log", "dated\n")
    _mkdir(guest, "/srv/outside", 0o755)
    _write(guest, "/srv/outside/secret.log", "secret\n")
    (guest / "var/log/seerr/seerr.log").symlink_to(guest / "srv/outside/secret.log")
    _mkdir(guest, "/var/log/alias", 0o755)
    _write(guest, "/var/log/alias/current.log", "current\n")
    (guest / "var/log/alias/ignored.log").symlink_to(
        guest / "srv/outside/secret.log"
    )
    _add_acl_tools(guest)
    request = _logging_request(
        {
            "seerr": _service_request(
                _layout_request(required=[_unit_payload("seerr.service")])
            )
        },
        {
            "seerr/dated": {
                "layouts": {
                    "default": _descriptor(
                        roots=["/var/log/seerr"],
                        paths=[
                            "/var/log/seerr/seerr-[0-9][0-9][0-9][0-9]-"
                            "[0-9][0-9]-[0-9][0-9].log"
                        ],
                    )
                }
            },
            "seerr/excluded": {
                "layouts": {
                    "default": _descriptor(
                        roots=["/var/log/alias"],
                        paths=["/var/log/alias/*.log"],
                        exclude_paths=["/var/log/alias/ignored.log"],
                    )
                }
            },
        },
    )
    facts = _probe_logging(
        guest, request, _systemd_runner({"seerr.service": "active"})
    )
    logging = facts["logging"]
    dated = logging["sources"]["seerr/dated"]["layouts"]["default"]
    assert dated["status"] == "ok"
    assert dated["matches"][0]["matched"] == 1
    assert [entry["path"] for entry in dated["matches"][0]["files"]] == [
        "/var/log/seerr/seerr-2026-10-26.log"
    ]
    excluded = logging["sources"]["seerr/excluded"]["layouts"]["default"]
    assert excluded["status"] == "ok"
    assert [entry["path"] for entry in excluded["matches"][0]["files"]] == [
        "/var/log/alias/current.log"
    ]
    assert "secret.log" not in json.dumps(logging)


def test_probe_logging_rejects_mount_escapes_the_reader_glob_covers(
    guest: Path,
) -> None:
    _mkdir(guest, "/var/log/demoapp/escape", 0o755)
    _write(guest, "/var/log/demoapp/escape/hidden.log", "hidden\n")
    _write(guest, "/var/log/demoapp/current.log", "current\n")
    _mkdir(guest, "/var/log/demoapp/nested", 0o755)
    _write(guest, "/var/log/demoapp/nested/deep.log", "deep\n")
    _mkdir(guest, "/proc/self", 0o755)
    mountinfo = guest / "proc/self/mountinfo"
    mountinfo.write_text(
        "1 0 0:1 / /var/log/demoapp/escape rw - tmpfs tmpfs rw\n"
    )
    _add_acl_tools(guest)
    nonmatching = _demo_request(
        descriptor=_descriptor(
            roots=["/var/log/demoapp"],
            paths=["/var/log/demoapp/*.log"],
        )
    )
    allowed = _probe_logging(
        guest, nonmatching, _systemd_runner({"demoapp.service": "active"})
    )
    allowed_source = allowed["logging"]["sources"]["demoapp/application"]["layouts"][
        "default"
    ]
    assert allowed_source["status"] == "ok"
    assert [entry["path"] for entry in allowed_source["matches"][0]["files"]] == [
        "/var/log/demoapp/current.log"
    ]

    matching = _demo_request(
        descriptor=_descriptor(
            roots=["/var/log/demoapp"],
            paths=["/var/log/demoapp/**/*.log"],
        )
    )
    failed = _probe_logging(
        guest, matching, _systemd_runner({"demoapp.service": "active"})
    )
    failed_source = failed["logging"]["sources"]["demoapp/application"]["layouts"][
        "default"
    ]
    assert failed_source["status"] == "failed"
    assert failed_source["matches"][0]["status"] == "failed"

    mountinfo.write_text(
        "1 0 0:1 / /var/log/demoapp/escape rw - tmpfs tmpfs rw\n"
        "2 1 0:2 / /var/log/demoapp/mounted.log rw - tmpfs tmpfs rw\n"
    )
    _write(guest, "/var/log/demoapp/mounted.log", "mounted\n")
    mounted = _probe_logging(
        guest, nonmatching, _systemd_runner({"demoapp.service": "active"})
    )
    mounted_source = mounted["logging"]["sources"]["demoapp/application"]["layouts"][
        "default"
    ]
    assert mounted_source["status"] == "failed"

    exact_request = _logging_request(
        {
            "demoapp": _service_request(
                _layout_request(required=[_unit_payload("demoapp.service")])
            )
        },
        {
            "demoapp/exact": {
                "layouts": {
                    "default": _descriptor(
                        exact_files=["/var/log/demoapp/mounted.log"]
                    )
                }
            }
        },
    )
    exact = _probe_logging(
        guest, exact_request, _systemd_runner({"demoapp.service": "active"})
    )
    exact_source = exact["logging"]["sources"]["demoapp/exact"]["layouts"][
        "default"
    ]
    assert exact_source["status"] == "failed"
    assert exact_source["exact_files"]["/var/log/demoapp/mounted.log"][
        "status"
    ] == "failed"

    excluded = _demo_request(
        descriptor=_descriptor(
            roots=["/var/log/demoapp"],
            paths=["/var/log/demoapp/**/*.log"],
            exclude_paths=[
                "/var/log/demoapp/escape",
                "/var/log/demoapp/mounted.log",
            ],
        )
    )
    excluded_case = _probe_logging(
        guest, excluded, _systemd_runner({"demoapp.service": "active"})
    )
    excluded_source = excluded_case["logging"]["sources"]["demoapp/application"][
        "layouts"
    ]["default"]
    assert excluded_source["status"] == "ok"
    assert sorted(
        entry["path"] for entry in excluded_source["matches"][0]["files"]
    ) == [
        "/var/log/demoapp/current.log",
        "/var/log/demoapp/nested/deep.log",
    ]


def test_probe_logging_truncation_and_bounded_file_lists(guest, monkeypatch) -> None:
    _mkdir(guest, "/var/log/demoapp", 0o755)
    _write(guest, "/var/log/demoapp/a.log", "a\n")
    _write(guest, "/var/log/demoapp/b.log", "b\n")
    _add_acl_tools(guest)
    request = _demo_request()
    runner = _systemd_runner({"demoapp.service": "active"})

    monkeypatch.setattr(hio, "_MAX_LOGGING_FILES", 1)
    bounded = _probe_logging(guest, request, runner)
    bounded_source = bounded["logging"]["sources"]["demoapp/application"][
        "layouts"
    ]["default"]
    match = bounded_source["matches"][0]
    assert bounded_source["status"] == "partial"
    assert bounded_source["truncated"] is True
    assert match["status"] == "partial"
    assert match["matched"] == 2
    assert len(match["files"]) == 1
    assert match["skipped"] == 0
    assert match["truncated"] is True
    _validate_logging_facts(bounded["logging"], request)

    monkeypatch.setattr(hio, "_MAX_LOGGING_ENTRIES", 1)
    partial = _probe_logging(guest, request, runner)
    source = partial["logging"]["sources"]["demoapp/application"]["layouts"][
        "default"
    ]
    assert source["truncated"] is True
    assert source["status"] == "partial"
    assert source["matches"][0]["truncated"] is True
    assert source["matches"][0]["status"] == "partial"
    assert source["matches"][0]["skipped"] == 0
    _validate_logging_facts(partial["logging"], request)


def test_probe_logging_large_source_keeps_full_identity_proof(
    tmp_path, guest: Path
) -> None:
    tasks = _mkdir(guest, "/var/log/proxmox-backup/tasks/AB", 0o755)
    count = 1_200
    for index in range(count):
        (tasks / "UPID:test:{0:08d}:00000000".format(index)).write_text("x\n")
    _add_acl_tools(guest)
    catalogue_dir = tmp_path / "catalogue"
    catalogue_dir.mkdir()
    (catalogue_dir / "pbslive.yml").write_text(
        """
schema_version: 1
id: pbslive
display_name: PBS-like large source
kind: app
community_script_ids: [pbslive]
journal_units: []
includes: []
layouts:
  - id: default
    required_units: [{name: proxmox-backup.service}]
    sources:
      - id: live-tasks
        app: pbslive
        log_kind: live-tasks
        allowed_roots: [/var/log/proxmox-backup/tasks]
        paths: ["/var/log/proxmox-backup/tasks/[0-9A-F][0-9A-F]/UPID:*"]
""",
        encoding="utf-8",
    )
    selection = lp.select_log_plugins(
        lp.load_log_catalogue(catalogue_dir),
        script_id="pbslive",
        override=None,
        auto=True,
    )
    request = lp.build_logging_request(selection)
    facts = _probe_logging(
        guest,
        request,
        _systemd_runner({"proxmox-backup.service": "active"}),
    )
    logging = facts["logging"]
    _validate_logging_facts(logging, request)
    match = logging["sources"]["pbslive/live-tasks"]["layouts"][
        "default"
    ]["matches"][0]
    assert match["status"] == "ok"
    assert match["truncated"] is False
    assert match["matched"] == count
    assert len(match["files"]) == count
    assert match["skipped"] == 0
    resolved = lp.validate_log_layout(selection, logging)
    assert resolved.complete is True
    assert resolved.sources[0].live_files == count


@pytest.mark.parametrize(
    ("script_id", "units", "tasks_root", "upid"),
    [
        (
            "proxmox-backup-server",
            ("proxmox-backup.service", "proxmox-backup-proxy.service"),
            "/var/log/proxmox-backup/tasks",
            "UPID:pbs:0000A1B2:00000000:00000000:00000000:backup:"
            "store1\\x3act-100:root@pam:",
        ),
        (
            "proxmox-datacenter-manager",
            (
                "proxmox-datacenter-api.service",
                "proxmox-datacenter-privileged-api.service",
            ),
            "/var/log/proxmox-datacenter-manager/tasks",
            "UPID:pdm:0000A1B2:00000000:00000000:00000000:backup:"
            "store1\\x3act-100:root@pam:",
        ),
    ],
)
def test_probe_logging_preserves_escaped_task_filename_identity(
    guest: Path, script_id: str, units, tasks_root: str, upid: str
) -> None:
    """Real probe output keeps PBS/PDM systemd-style backslash escapes verbatim.

    The helper observes the raw filename; validating it as a resolved source
    must not treat the literal ``\\x3a`` as an authored exact-file path.
    """
    _add_acl_tools(guest)
    _mkdir(guest, f"{tasks_root}/AB", 0o755)
    task = _write(guest, f"{tasks_root}/AB/{upid}", "task output\n")
    if script_id == "proxmox-backup-server":
        _mkdir(guest, "/var/log/proxmox-backup/api", 0o755)
        _write(guest, "/var/log/proxmox-backup/api/access.log", "api access\n")
        _write(guest, "/var/log/proxmox-backup/api/auth.log", "api auth\n")

    catalogue = lp.load_log_catalogue()
    selection = lp.select_log_plugins(
        catalogue, script_id=script_id, override=None, auto=True
    )
    assert selection.status == "selected"
    request = lp.build_logging_request(selection)
    facts = _probe_logging(
        guest, request, _systemd_runner({unit: "active" for unit in units})
    )
    logging = facts["logging"]
    _validate_logging_facts(logging, request)

    resolved = lp.validate_log_layout(selection, logging)

    assert resolved.complete is True
    tasks = next(
        source for source in resolved.sources if source.source_id == "tasks"
    )
    assert tasks.status == "active"
    assert tasks.live_files == 1
    record = logging["sources"][f"{script_id}/tasks"]["layouts"]["default"][
        "matches"
    ][0]["files"][0]
    assert "\\x3a" in record["path"]
    assert record["path"] == f"{tasks_root}/AB/{upid}"
    assert task.name == upid
    assert task.is_file()
    observed = task.lstat()
    assert (record["device"], record["inode"]) == (observed.st_dev, observed.st_ino)


def test_probe_logging_serialized_response_budget_marks_partial(
    guest: Path, monkeypatch
) -> None:
    _mkdir(guest, "/var/log/demoapp", 0o755)
    for index in range(6):
        _write(guest, "/var/log/demoapp/{0}.log".format(index), "x\n")
    _add_acl_tools(guest)
    request = _demo_request()
    runner = _systemd_runner({"demoapp.service": "active"})
    baseline = _probe_logging(guest, request, runner)
    baseline_bytes = len(
        json.dumps(baseline["logging"], separators=(",", ":"))
    )
    monkeypatch.setattr(
        hio, "_MAX_LOGGING_RESPONSE_BYTES", baseline_bytes - 1
    )
    capped = _probe_logging(guest, request, runner)
    logging = capped["logging"]
    _validate_logging_facts(logging, request)
    source = logging["sources"]["demoapp/application"]["layouts"]["default"]
    assert source["status"] == "partial"
    assert source["truncated"] is True
    assert source["matches"][0]["status"] == "partial"
    assert source["matches"][0]["truncated"] is True
    assert source["matches"][0]["files"] == []
    assert source["matches"][0]["skipped"] == 0


def test_probe_logging_failed_enumeration_is_not_quiet(guest, monkeypatch) -> None:
    _mkdir(guest, "/var/log/demoapp/child", 0o755)
    _write(guest, "/var/log/demoapp/child/a.log", "a\n")
    _add_acl_tools(guest)
    request = _demo_request(
        descriptor=_descriptor(
            roots=["/var/log/demoapp"],
            paths=["/var/log/demoapp/**/*.log"],
        )
    )
    real_scandir = os.scandir
    calls = {"count": 0}

    def guard(path):
        calls["count"] += 1
        if calls["count"] > 1:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(hio.os, "scandir", guard)
    facts = _probe_logging(
        guest, request, _systemd_runner({"demoapp.service": "active"})
    )
    source = facts["logging"]["sources"]["demoapp/application"]["layouts"][
        "default"
    ]
    assert source["status"] == "failed"
    assert source["roots"]["/var/log/demoapp"]["status"] == "failed"
    assert source["matches"][0]["status"] == "failed"
    assert source["matches"][0]["status"] != "quiet"


def test_probe_logging_unit_scopes_glob_instances_and_unavailable(guest: Path) -> None:
    _write(
        guest,
        "/etc/passwd",
        "root:x:0:0:root:/root:/bin/bash\n"
        "alloy:x:991:991::/var/lib/alloy:/usr/sbin/nologin\n"
        "hermes:x:1000:1000::/home/hermes:/bin/bash\n",
    )
    _mkdir(guest, "/run/user/1000", 0o755)
    _add_acl_tools(guest)
    request = _logging_request(
        {
            "hermesagent": _service_request(
                _layout_request(
                    required=[
                        _unit_payload("hermes-dashboard.service"),
                        _unit_payload(
                            "postgresql@*-main.service",
                            scope="user",
                            user="hermes",
                        ),
                    ],
                    running_any=[
                        _unit_payload(
                            "hermes-gateway.service", scope="user", user="hermes"
                        )
                    ],
                ),
                journal_units=[_unit_payload("optional.timer")],
            )
        },
        {},
    )
    user_base = (
        "runuser -u hermes -- env XDG_RUNTIME_DIR=/run/user/1000 "
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
        "systemctl --user "
    )
    user_prefix = (
        user_base
        + "show --no-pager --property=LoadState --property=ActiveState -- "
    )
    extra = {
        user_base
        + "list-unit-files --no-legend --no-pager -- "
        "postgresql@*-main.service": (
            0,
            "postgresql@15-main.service enabled\n"
            "postgresql@16-main.service disabled\n",
            "",
        ),
        user_prefix + "postgresql@15-main.service": (
            0,
            "LoadState=loaded\nActiveState=active\n",
            "",
        ),
        user_prefix + "postgresql@16-main.service": (
            0,
            "LoadState=loaded\nActiveState=inactive\n",
            "",
        ),
        user_prefix + "hermes-gateway.service": (
            0,
            "LoadState=loaded\nActiveState=active\n",
            "",
        ),
    }
    runner = _systemd_runner(
        {
            "hermes-dashboard.service": "active",
            "optional.timer": "inactive",
        },
        extra=extra,
    )
    facts = _probe_logging(guest, request, runner)
    units = facts["logging"]["plugins"]["hermesagent"]["units"]
    assert units["system:hermes-dashboard.service"]["status"] == "active"
    glob = units["user:hermes:postgresql@*-main.service"]
    assert glob["status"] == "active"
    assert glob["instances"] == {
        "postgresql@15-main.service": "active",
        "postgresql@16-main.service": "inactive",
    }
    assert units["user:hermes:hermes-gateway.service"]["status"] == "active"
    assert units["system:optional.timer"]["status"] == "inactive"
    _validate_logging_facts(facts["logging"], request)

    zero = _logging_request(
        {
            "hermesagent": _service_request(
                _layout_request(
                    required=[_unit_payload("postgresql@*-main.service")]
                )
            )
        },
        {},
    )
    missing = _probe_logging(
        guest,
        zero,
        _systemd_runner(
            extra={
                "systemctl list-unit-files --no-legend --no-pager -- "
                "postgresql@*-main.service": (0, "", "")
            }
        ),
    )
    assert (
        missing["logging"]["plugins"]["hermesagent"]["units"][
            "system:postgresql@*-main.service"
        ]["status"]
        == "missing"
    )

    unavailable = _probe_logging(
        guest,
        _logging_request(
            {
                "demoapp": _service_request(
                    _layout_request(
                        required=[
                            _unit_payload("demoapp.service"),
                            _unit_payload(
                                "dashboard.service", scope="user", user="ghost"
                            ),
                        ]
                    )
                )
            },
            {},
        ),
        _systemd_runner({"demoapp.service": "active"}),
    )
    units = unavailable["logging"]["plugins"]["demoapp"]["units"]
    assert units["system:demoapp.service"]["status"] == "active"
    assert units["user:ghost:dashboard.service"]["status"] == "unavailable"
    assert units["user:ghost:dashboard.service"]["instances"] == {}

    os.rmdir(guest / "run/user/1000")
    no_manager = _probe_logging(
        guest,
        _logging_request(
            {
                "hermesagent": _service_request(
                    _layout_request(
                        required=[
                            _unit_payload(
                                "dashboard.service", scope="user", user="hermes"
                            )
                        ]
                    )
                )
            },
            {},
        ),
        _systemd_runner(),
    )
    assert (
        no_manager["logging"]["plugins"]["hermesagent"]["units"][
            "user:hermes:dashboard.service"
        ]["status"]
        == "unavailable"
    )


def test_probe_logging_keeps_layout_variants_separate_and_resolves(
    tmp_path, guest: Path
) -> None:
    _mkdir(guest, "/var/log/legacy", 0o755)
    _write(guest, "/var/log/legacy/legacy.log", "legacy\n")
    _mkdir(guest, "/var/log/v2", 0o755)
    _write(guest, "/var/log/v2/current.log", "v2\n")
    _add_acl_tools(guest)
    catalogue_dir = tmp_path / "catalogue"
    catalogue_dir.mkdir()
    (catalogue_dir / "demoapp.yml").write_text(
        """
schema_version: 1
id: demoapp
display_name: Demo app
kind: app
community_script_ids: [demoapp]
journal_units: []
includes: []
layouts:
  - id: legacy
    required_units: [{name: legacy.service}]
    sources:
      - id: application
        app: demoapp
        log_kind: application
        allowed_roots: [/var/log/legacy]
        paths: ["/var/log/legacy/*.log"]
  - id: v2
    required_units: [{name: v2.service}]
    sources:
      - id: application
        app: demoapp
        log_kind: application
        allowed_roots: [/var/log/v2]
        paths: ["/var/log/v2/*.log"]
""",
        encoding="utf-8",
    )
    selection = lp.select_log_plugins(
        lp.load_log_catalogue(catalogue_dir),
        script_id="demoapp",
        override=None,
        auto=True,
    )
    request = lp.build_logging_request(selection)
    facts = _probe_logging(
        guest,
        request,
        _systemd_runner({"legacy.service": "inactive", "v2.service": "active"}),
    )
    logging = facts["logging"]
    _validate_logging_facts(logging, request)
    layouts = logging["sources"]["demoapp/application"]["layouts"]
    assert set(layouts) == {"legacy", "v2"}
    assert set(layouts["legacy"]["roots"]) == {"/var/log/legacy"}
    assert set(layouts["v2"]["roots"]) == {"/var/log/v2"}
    assert layouts["v2"]["matches"][0]["files"][0]["path"] == "/var/log/v2/current.log"
    resolved = lp.validate_log_layout(selection, logging)
    assert resolved.complete is True
    assert resolved.services[0].layout_id == "v2"
    assert resolved.sources[0].paths == ("/var/log/v2/*.log",)
    assert resolved.sources[0].status == "active"


def test_probe_logging_reports_unmeasurable_separately_from_denied(
    guest: Path, monkeypatch
) -> None:
    _mkdir(guest, "/var/log/demoapp", 0o755)
    _write(guest, "/var/log/demoapp/a.log", "a\n")
    _add_acl_tools(guest)
    request = _demo_request()
    monkeypatch.setattr(hio, "_read_mountpoints", lambda _sysroot: None)
    mountless = _probe_logging(
        guest, request, _systemd_runner({"demoapp.service": "active"})
    )
    mountless_source = mountless["logging"]["sources"]["demoapp/application"][
        "layouts"
    ]["default"]
    assert mountless_source["status"] == "unavailable"
    assert (
        mountless_source["roots"]["/var/log/demoapp"]["status"] == "unavailable"
    )

    monkeypatch.setattr(hio, "_read_mountpoints", lambda _sysroot: set())
    _write(guest, "/etc/passwd", "root:x:0:0:root:/root:/bin/bash\n")
    facts = _probe_logging(
        guest, request, _systemd_runner({"demoapp.service": "active"})
    )
    source = facts["logging"]["sources"]["demoapp/application"]["layouts"][
        "default"
    ]
    assert source["status"] == "unavailable"
    assert source["roots"]["/var/log/demoapp"]["status"] == "unavailable"
    assert "access" not in source["roots"]["/var/log/demoapp"]


def test_probe_logging_absent_sources_match_pure_layout_semantics(
    tmp_path, guest: Path
) -> None:
    _mkdir(guest, "/var/log/demoapp", 0o755)
    _add_acl_tools(guest)
    catalogue_dir = tmp_path / "catalogue"
    catalogue_dir.mkdir()
    (catalogue_dir / "demoapp.yml").write_text(
        """
schema_version: 1
id: demoapp
display_name: Demo app
kind: app
community_script_ids: [demoapp]
journal_units: []
includes: []
layouts:
  - id: default
    required_units: [{name: demoapp.service}]
    sources:
      - id: optional-root
        app: demoapp
        log_kind: optional-root
        allowed_roots: [/var/log/absent-optional]
        paths: ["/var/log/absent-optional/*.log"]
        required: false
      - id: required-root
        app: demoapp
        log_kind: required-root
        allowed_roots: [/var/log/absent-required]
        paths: ["/var/log/absent-required/*.log"]
      - id: optional-exact
        app: demoapp
        log_kind: optional-exact
        exact_files: [/opt/demoapp/missing.log]
        required: false
      - id: future-exact
        app: demoapp
        log_kind: future-exact
        exact_files: [/var/log/demoapp/missing.log]
""",
        encoding="utf-8",
    )
    selection = lp.select_log_plugins(
        lp.load_log_catalogue(catalogue_dir),
        script_id="demoapp",
        override=None,
        auto=True,
    )
    request = lp.build_logging_request(selection)
    facts = _probe_logging(
        guest, request, _systemd_runner({"demoapp.service": "active"})
    )
    logging = facts["logging"]
    _validate_logging_facts(logging, request)
    optional = logging["sources"]["demoapp/optional-root"]["layouts"]["default"]
    required = logging["sources"]["demoapp/required-root"]["layouts"]["default"]
    optional_exact = logging["sources"]["demoapp/optional-exact"]["layouts"][
        "default"
    ]
    future_exact = logging["sources"]["demoapp/future-exact"]["layouts"]["default"]
    assert optional["roots"]["/var/log/absent-optional"]["status"] == "missing_root"
    assert optional["status"] == "missing_root"
    assert required["roots"]["/var/log/absent-required"]["status"] == "missing_root"
    assert required["status"] == "failed"
    assert optional_exact["exact_files"]["/opt/demoapp/missing.log"][
        "status"
    ] == "missing_root"
    assert optional_exact["status"] == "missing_root"
    assert future_exact["exact_files"]["/var/log/demoapp/missing.log"][
        "parent_traversable"
    ] is True
    assert future_exact["status"] == "ok"

    resolved = lp.validate_log_layout(selection, logging)
    by_key = {source.key: source for source in resolved.sources}
    assert by_key["demoapp/optional-root"].status == "inactive"
    assert by_key["demoapp/required-root"].status == "blocked"
    assert by_key["demoapp/optional-exact"].status == "inactive"
    assert by_key["demoapp/future-exact"].status == "quiet"
    assert by_key["demoapp/future-exact"].renderable is True
    assert resolved.complete is False


def test_probe_logging_cross_owner_path_conflict_is_rejected_by_resolver(
    tmp_path, guest: Path
) -> None:
    _mkdir(guest, "/var/log/shared", 0o755)
    _write(guest, "/var/log/shared/app.log", "shared\n")
    _add_acl_tools(guest)
    catalogue_dir = tmp_path / "catalogue"
    catalogue_dir.mkdir()
    (catalogue_dir / "demoapp.yml").write_text(
        """
schema_version: 1
id: demoapp
display_name: Demo app
kind: app
community_script_ids: [demoapp]
journal_units: []
includes: [demodep]
layouts:
  - id: default
    required_units: [{name: demoapp.service}]
    sources:
      - id: application
        app: demoapp
        log_kind: application
        exact_files: [/var/log/shared/app.log]
""",
        encoding="utf-8",
    )
    (catalogue_dir / "demodep.yml").write_text(
        """
schema_version: 1
id: demodep
display_name: Demo dependency
kind: dependency
community_script_ids: []
journal_units: []
includes: []
layouts:
  - id: default
    required_units: [{name: demodep.service}]
    sources:
      - id: error
        app: demodep
        log_kind: error
        exact_files: [/var/log/shared/app.log]
""",
        encoding="utf-8",
    )
    selection = lp.select_log_plugins(
        lp.load_log_catalogue(catalogue_dir),
        script_id="demoapp",
        override=None,
        auto=True,
    )
    request = lp.build_logging_request(selection)
    facts = _probe_logging(
        guest,
        request,
        _systemd_runner(
            {"demoapp.service": "active", "demodep.service": "active"}
        ),
    )
    logging = facts["logging"]
    _validate_logging_facts(logging, request)
    with pytest.raises(lp.LogLayoutError, match="ownership conflict"):
        lp.validate_log_layout(selection, logging)


def test_probe_logging_hardlink_identity_conflict_is_rejected_by_resolver(
    tmp_path, guest: Path
) -> None:
    _mkdir(guest, "/var/log/first", 0o755)
    _mkdir(guest, "/var/log/second", 0o755)
    first = _write(guest, "/var/log/first/a.log", "same inode\n")
    os.link(first, guest / "var/log/second/b.log")
    _add_acl_tools(guest)
    catalogue_dir = tmp_path / "catalogue"
    catalogue_dir.mkdir()
    (catalogue_dir / "demoapp.yml").write_text(
        """
schema_version: 1
id: demoapp
display_name: Demo app
kind: app
community_script_ids: [demoapp]
journal_units: []
includes: []
layouts:
  - id: default
    required_units: [{name: demoapp.service}]
    sources:
      - id: first
        app: demoapp
        log_kind: first
        allowed_roots: [/var/log/first]
        paths: ["/var/log/first/*.log"]
      - id: second
        app: demoapp
        log_kind: second
        allowed_roots: [/var/log/second]
        paths: ["/var/log/second/*.log"]
""",
        encoding="utf-8",
    )
    selection = lp.select_log_plugins(
        lp.load_log_catalogue(catalogue_dir),
        script_id="demoapp",
        override=None,
        auto=True,
    )
    request = lp.build_logging_request(selection)
    facts = _probe_logging(
        guest, request, _systemd_runner({"demoapp.service": "active"})
    )
    logging = facts["logging"]
    _validate_logging_facts(logging, request)
    with pytest.raises(lp.LogLayoutError, match="identity"):
        lp.validate_log_layout(selection, logging)


def test_archive_requests_reject_declarative_plugin_fields(
    tmp_path, guest: Path
) -> None:
    assert guest.is_dir()
    with pytest.raises(hio.HelperError, match="declarative logging"):
        hio.capture(
            {"capture_id": CAPTURE_ID, "logging": {}, "files": []},
            sysroot=str(guest),
            spool_root=str(tmp_path / "spool"),
        )
    with pytest.raises(hio.HelperError, match="declarative logging"):
        hio.capture(
            {
                "capture_id": CAPTURE_ID,
                "files": [{"path": "/data/logs/a.log", "app": "demoapp"}],
            },
            sysroot=str(guest),
            spool_root=str(tmp_path / "spool"),
        )
    with pytest.raises(hio.HelperError, match="declarative logging"):
        hio.prune(
            {"logging": {}, "files": []},
            sysroot=str(guest),
        )
    with pytest.raises(hio.HelperError, match="declarative logging"):
        hio.snapshot(
            {"files": [{"path": "/data/logs/a.log", "app": "demoapp"}]},
            destination=str(tmp_path / "snapshot.tar"),
            sysroot=str(guest),
        )


def test_probe_logging_fact_validation_requires_exact_request_parity(
    guest: Path,
) -> None:
    _mkdir(guest, "/var/log/demoapp", 0o755)
    _write(guest, "/var/log/demoapp/a.log", "a\n")
    _add_acl_tools(guest)
    request = _demo_request()
    logging = _probe_logging(
        guest, request, _systemd_runner({"demoapp.service": "active"})
    )["logging"]
    _validate_logging_facts(logging, request)
    with pytest.raises(HousekeepingError, match="unsolicited logging"):
        _validate_logging_facts(logging, None)
    extra = json.loads(json.dumps(logging))
    extra["plugins"]["demoapp"]["units"]["system:extra.service"] = {
        "status": "active",
        "instances": {},
    }
    with pytest.raises(HousekeepingError, match="unit evidence"):
        _validate_logging_facts(extra, request)
    missing = json.loads(json.dumps(logging))
    missing["sources"].pop("demoapp/application")
    with pytest.raises(HousekeepingError, match="source evidence"):
        _validate_logging_facts(missing, request)
    bad = json.loads(json.dumps(logging))
    bad["sources"]["demoapp/application"]["layouts"]["default"]["matches"][0][
        "status"
    ] = "running"
    with pytest.raises(HousekeepingError, match="match status"):
        _validate_logging_facts(bad, request)


# --------------------------------------------------------------------------- #
# bounded rootfs disk usage (read-only diagnostics)
# --------------------------------------------------------------------------- #


def _disk_request(**over: object) -> dict:
    request: dict = {
        "max_depth": 3,
        "max_entries": 200000,
        "timeout_seconds": 15,
        "extra_paths": [],
    }
    request.update(over)
    return request


def _disk_mountinfo(
    tmp_path: Path, root: Path, mounts=(), *, mount_id_override=None
) -> Path:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        root_mount = (
            mount_id_override
            if mount_id_override is not None
            else hio._fd_mount_id(fd)
        )
    finally:
        os.close(fd)
    assert root_mount is not None
    lines = [f"{root_mount} 0 0:1 / / rw - ext4 /dev/root rw"]
    for index, mount in enumerate(mounts, start=1):
        escaped = (
            mount.replace("\\", "\\134")
            .replace(" ", "\\040")
            .replace("\t", "\\011")
            .replace("\n", "\\012")
        )
        lines.append(
            f"{root_mount + 100 + index} 0 0:2 / {escaped} rw - tmpfs tmpfs rw"
        )
    path = tmp_path / "mountinfo"
    path.write_text("\n".join(lines) + "\n")
    return path


def _disk_call(
    tmp_path: Path,
    guest: Path,
    *,
    extra_paths=(),
    mounts=(),
    mountinfo: Path | None = None,
    **over: object,
) -> dict:
    info = mountinfo or _disk_mountinfo(tmp_path, guest, mounts)
    request = _disk_request(**over)
    if "extra_paths" not in over:
        request["extra_paths"] = list(extra_paths)
    return hio.disk_usage(
        request,
        sysroot=str(guest),
        mountinfo_path=str(info),
    )


def _alloc(path: Path) -> int:
    return os.lstat(path).st_blocks * 512


def _rows(result: dict) -> dict:
    return {row["path"]: row for row in result["directories"]}


def test_disk_usage_counts_allocated_blocks_and_reports_depth_three(
    tmp_path: Path,
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/root-direct.bin", b"r" * 3000)
    log_a = _write(guest, "/var/log/app.log", b"a" * 5000)
    log_b = _write(guest, "/var/log/deep/a/b/c.log", b"b" * 9000)
    cache = _write(guest, "/var/cache/blob", b"c" * 2000)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * 40000)
    link = guest / "var/log/outside-link"
    link.symlink_to(outside)

    result = _disk_call(tmp_path, guest)

    assert result["status"] == "complete", result
    assert isinstance(result["root_used_bytes"], int)
    assert result["root_used_bytes"] >= 0
    assert result["root_device"] == os.stat(guest).st_dev
    rows = _rows(result)
    root = rows["/"]
    assert root["depth"] == 0
    assert root["allocated_bytes"] == root["direct_bytes"] == _alloc(
        guest / "root-direct.bin"
    )
    assert root["allocated_bytes"] != result["root_used_bytes"]
    var_log = rows["/var/log"]
    assert var_log["depth"] == 2
    assert var_log["direct_bytes"] == _alloc(log_a)
    assert var_log["allocated_bytes"] == _alloc(log_a) + _alloc(log_b)
    assert rows["/var/cache"]["allocated_bytes"] == _alloc(cache)
    assert rows["/var"]["allocated_bytes"] >= var_log["allocated_bytes"]
    # Deeper-than-report-depth directories never become unsolicited rows.
    assert not any(row["depth"] > 3 for row in result["directories"])
    assert "/var/log/outside-link" not in rows
    assert result["visited_entries"] >= 6
    assert result["excluded_mounts"] == 0
    assert result["error_count"] == 0


def test_disk_usage_counts_a_hardlink_once_and_attributes_the_first_path(
    tmp_path: Path,
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/data", 0o755)
    original = _write(guest, "/data/a/payload.bin", b"p" * 12000)
    (guest / "data/b").mkdir(parents=True)
    os.link(original, guest / "data/b/payload.bin")

    result = _disk_call(tmp_path, guest)

    assert result["status"] == "complete", result
    rows = _rows(result)
    owned = [
        rows[path]["allocated_bytes"]
        for path in ("/data/a", "/data/b")
        if path in rows
    ]
    assert sum(owned) == _alloc(original)
    assert _alloc(original) in owned


def test_disk_usage_prunes_every_mountpoint_before_opening_it(
    tmp_path: Path,
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/app.log", b"a" * 4000)
    mounted_file = _write(guest, "/var/log/mounted.log", b"m" * 7000)
    _write(guest, "/mnt/share/big.bin", b"n" * 20000)
    _write(guest, "/mnt space dir/escaped.bin", b"e" * 6000)

    result = _disk_call(
        tmp_path, guest, mounts=["/mnt", "/mnt space dir", "/var/log/mounted.log"]
    )

    assert result["status"] == "complete", result
    assert result["excluded_mounts"] == 3
    rows = _rows(result)
    assert "/mnt" not in rows
    assert "/mnt space dir" not in rows
    assert rows["/var/log"]["allocated_bytes"] == _alloc(
        guest / "var/log/app.log"
    )
    assert rows["/var/log"]["direct_bytes"] == _alloc(
        guest / "var/log/app.log"
    )
    assert _alloc(mounted_file) > 0


def test_disk_usage_excludes_a_same_device_bind_that_is_missing_from_the_table(
    tmp_path: Path, monkeypatch
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/data", 0o755)
    normal = _write(guest, "/data/normal.bin", b"n" * 1500)
    _write(guest, "/data/bind/hidden.bin", b"h" * 12000)
    info = _disk_mountinfo(tmp_path, guest)
    real_mount_id = hio._fd_mount_id

    def fake_mount_id(fd: int):
        observed = real_mount_id(fd)
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return observed
        if target.rstrip("/").endswith("/data/bind"):
            return (observed or 0) + 1
        return observed

    monkeypatch.setattr(hio, "_fd_mount_id", fake_mount_id)

    result = _disk_call(tmp_path, guest, mountinfo=info)

    assert result["status"] == "complete", result
    assert result["excluded_mounts"] == 1
    rows = _rows(result)
    assert "/data/bind" not in rows
    assert rows["/data"]["allocated_bytes"] == _alloc(normal)
    assert result["error_count"] == 0


def test_disk_usage_measures_requested_deep_extra_paths_with_ancestry(
    tmp_path: Path,
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    store = _write(
        guest, "/root/.local/share/pnpm/store/v3/pkg/index.json", b"s" * 8000
    )
    _write(guest, "/root/.local/share/pnpm/other.bin", b"o" * 3000)

    result = _disk_call(
        tmp_path, guest, extra_paths=["/root/.local/share/pnpm/store"]
    )

    assert result["status"] == "complete", result
    rows = _rows(result)
    assert rows["/root/.local/share/pnpm/store"]["depth"] == 5
    assert rows["/root/.local/share/pnpm/store"]["allocated_bytes"] == _alloc(
        store
    )
    for ancestor in ("/root", "/root/.local", "/root/.local/share"):
        assert ancestor in rows
    # Only depth<=3 ancestors and the requested path are on the wire.
    assert "/root/.local/share/pnpm" not in rows
    assert all(
        row["depth"] <= 3 or row["path"] == "/root/.local/share/pnpm/store"
        for row in result["directories"]
    )


def test_disk_usage_never_fabricates_unvisited_extra_paths(tmp_path: Path) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/a.log", b"a" * 1000)

    result = _disk_call(
        tmp_path, guest, extra_paths=["/opt/missing/store", "/var/log/a.log"]
    )

    assert result["status"] == "complete", result
    rows = _rows(result)
    assert "/opt/missing/store" not in rows
    # A requested extra that is a file, not a directory, is not invented.
    assert "/var/log/a.log" not in rows


@pytest.mark.parametrize(
    "over",
    [
        {"unknown": 1},
        {"max_depth": 4},
        {"max_depth": True},
        {"max_depth": -1},
        {"max_entries": 0},
        {"max_entries": 200001},
        {"max_entries": False},
        {"timeout_seconds": 0},
        {"timeout_seconds": 15.5},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": "15"},
        {"timeout_seconds": True},
        {"extra_paths": "not-a-list"},
        {"extra_paths": ["/ok", "/ok"]},
        {"extra_paths": ["relative/path"]},
        {"extra_paths": ["/"]},
        {"extra_paths": ["//double"]},
        {"extra_paths": ["/a/../b"]},
        {"extra_paths": ["/a//b"]},
        {"extra_paths": ["/a/"]},
        {"extra_paths": ["/a*glob"]},
        {"extra_paths": ["/a://uri"]},
        {"extra_paths": ["/a\x00b"]},
        {"extra_paths": ["/a" * 600]},
        {"extra_paths": ["/" + "/".join(["p"] * 17)]},
        {"extra_paths": [f"/p{i}" for i in range(33)]},
    ],
)
def test_disk_usage_request_validation_rejects_widening(
    tmp_path: Path, over: dict
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    with pytest.raises(hio.HelperError):
        _disk_call(tmp_path, guest, **over)


def test_disk_usage_request_requires_the_exact_operational_scope(
    tmp_path: Path,
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    info = _disk_mountinfo(tmp_path, guest)
    for request in (
        {},
        {"max_depth": 3, "max_entries": 200000, "timeout_seconds": 15},
        {"max_depth": 3, "max_entries": 200000, "extra_paths": []},
        {"max_depth": 3, "timeout_seconds": 15, "extra_paths": []},
        {"max_entries": 200000, "timeout_seconds": 15, "extra_paths": []},
    ):
        with pytest.raises(hio.HelperError):
            hio.disk_usage(
                request, sysroot=str(guest), mountinfo_path=str(info)
            )
    with pytest.raises(hio.HelperError):
        hio.disk_usage(
            _disk_request(sysroot=str(guest)),
            sysroot=str(guest),
            mountinfo_path=str(info),
        )


def test_disk_usage_unavailable_without_a_mount_table_or_mount_id(
    tmp_path: Path, monkeypatch
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/a.log", b"a" * 1000)
    missing = tmp_path / "missing-mountinfo"

    result = hio.disk_usage(
        _disk_request(), sysroot=str(guest), mountinfo_path=str(missing)
    )
    assert result["status"] == "unavailable"
    assert result["directories"] == []
    assert result["root_used_bytes"] is None
    assert result["reason"]

    info = _disk_mountinfo(tmp_path, guest)
    monkeypatch.setattr(hio, "_fd_mount_id", lambda fd: None)
    result = hio.disk_usage(
        _disk_request(), sysroot=str(guest), mountinfo_path=str(info)
    )
    assert result["status"] == "unavailable"
    assert result["directories"] == []


def test_disk_usage_rejects_a_symlinked_guest_root(tmp_path: Path) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/a.log", b"a" * 1000)
    info = _disk_mountinfo(tmp_path, guest)
    link = tmp_path / "link-root"
    link.symlink_to(guest, target_is_directory=True)

    result = hio.disk_usage(
        _disk_request(), sysroot=str(link), mountinfo_path=str(info)
    )

    assert result["status"] == "unavailable"
    assert result["directories"] == []


def test_disk_usage_marks_a_changed_mount_table_partial(
    tmp_path: Path, monkeypatch
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/a.log", b"a" * 1000)
    info = _disk_mountinfo(tmp_path, guest)
    real_table = hio._disk_mount_table
    observations = {"calls": 0}

    def flaky_table(path: str):
        observations["calls"] += 1
        table = real_table(path)
        if observations["calls"] == 1:
            return table
        return (table or []) + [(99999, "/appeared")]

    monkeypatch.setattr(hio, "_disk_mount_table", flaky_table)

    result = _disk_call(tmp_path, guest, mountinfo=info)

    assert result["status"] == "partial"
    assert "mount table" in result["reason"]
    assert _rows(result)["/var/log"]["allocated_bytes"] > 0


def test_disk_usage_entry_limit_is_partial_and_never_a_normal_zero(
    tmp_path: Path,
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/root.bin", b"r" * 3000)
    for index in range(20):
        _write(guest, f"/var/log/{index}.log", b"a" * 1000)

    result = _disk_call(tmp_path, guest, max_entries=5)

    assert result["status"] == "partial"
    assert result["visited_entries"] <= 5
    assert "entry limit" in result["reason"]
    assert result["directories"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a 000 directory")
def test_disk_usage_metadata_failures_are_partial_not_zero(
    tmp_path: Path,
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/a.log", b"a" * 1000)
    locked = _mkdir(guest, "/locked", 0o700)
    _write(guest, "/locked/secret.bin", b"s" * 5000)
    locked.chmod(0o000)
    try:
        result = _disk_call(tmp_path, guest)
    finally:
        locked.chmod(0o700)

    assert result["status"] == "partial"
    assert result["error_count"] >= 1
    assert "/locked" not in _rows(result)
    assert _rows(result)["/var/log"]["allocated_bytes"] == _alloc(
        guest / "var/log/a.log"
    )


def test_disk_usage_physical_depth_limit_is_partial(tmp_path: Path) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    current = guest
    for index in range(132):
        current = current / f"d{index}"
        current.mkdir()
    (current / "deep.bin").write_bytes(b"d" * 1000)

    result = _disk_call(tmp_path, guest)

    assert result["status"] == "partial"
    assert "depth" in result["reason"]
    assert all(row["depth"] <= 3 for row in result["directories"])


def test_disk_usage_row_budget_keeps_reserved_extras_and_ancestry(
    tmp_path: Path,
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    extras = []
    for index in range(32):
        extra = f"/x{index}/a/b/store"
        extras.append(extra)
        _write(guest, extra + "/pkg.bin", b"e" * 512)
    for index in range(1100):
        _write(guest, f"/base/d{index:04d}/f.bin", b"o" * 512)

    result = _disk_call(tmp_path, guest, extra_paths=extras)

    assert result["status"] == "partial"
    assert len(result["directories"]) <= 1000
    rows = _rows(result)
    assert "/" in rows
    for extra in extras:
        assert extra in rows
        parts = [part for part in extra.split("/") if part]
        for depth in range(1, 4):
            assert "/" + "/".join(parts[:depth]) in rows
    assert len(
        [row for row in result["directories"] if row["path"].startswith("/base/d")]
    ) >= 800
    encoded = json.dumps(result, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= 1024 * 1024


def test_disk_usage_progress_snapshots_never_replace_the_final_result(
    tmp_path: Path, monkeypatch
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/a.log", b"a" * 2000)
    _write(guest, "/var/log/b.log", b"b" * 1000)
    monkeypatch.setattr(hio, "DISK_USAGE_PROGRESS_SECONDS", 0.0)

    result = _disk_call(tmp_path, guest)

    assert result["status"] == "complete", result
    assert _rows(result)["/var/log"]["allocated_bytes"] == _alloc(
        guest / "var/log/a.log"
    ) + _alloc(guest / "var/log/b.log")


def test_disk_usage_blocked_worker_is_killed_within_the_bounded_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/a.log", b"a" * 1000)
    info = _disk_mountinfo(tmp_path, guest)

    def block(**_kwargs) -> None:
        time.sleep(10.0)

    # The fork inherits this monkeypatch, so the real _disk_worker closes
    # stdio and the real _disk_collect watchdog is exercised unchanged.
    monkeypatch.setattr(hio, "_disk_worker_main", block)

    started = time.monotonic()
    result = hio.disk_usage(
        _disk_request(timeout_seconds=0.2),
        sysroot=str(guest),
        mountinfo_path=str(info),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert result["status"] == "unavailable"
    assert result["directories"] == []
    assert result["reason"]


def test_cli_disk_usage_emits_one_bounded_snapshot(tmp_path: Path) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/a.log", b"a" * 4096)
    _write(guest, "/root/.local/share/pnpm/store/pkg.bin", b"s" * 8192)
    info = _disk_mountinfo(tmp_path, guest)
    request = tmp_path / "disk-request.json"
    request.write_text(
        json.dumps(_disk_request(extra_paths=["/root/.local/share/pnpm/store"]))
    )

    proc = _run_cli(
        [
            "--operation",
            "disk_usage",
            "--lxc-id",
            "135",
            "--request",
            str(request),
            "--sysroot",
            str(guest),
            "--mountinfo",
            str(info),
        ]
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count("\n") == 1
    payload = json.loads(proc.stdout)
    assert payload["status"] == "complete"
    rows = {row["path"]: row for row in payload["directories"]}
    assert rows["/root/.local/share/pnpm/store"]["depth"] == 5
    assert rows["/var/log"]["allocated_bytes"] == _alloc(
        guest / "var/log/a.log"
    )


def test_cli_disk_usage_requires_paired_fixture_flags(tmp_path: Path) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/a.log", b"a" * 1000)
    info = _disk_mountinfo(tmp_path, guest)
    request = tmp_path / "disk-request.json"
    request.write_text(json.dumps(_disk_request()))

    unpaired = _run_cli(
        [
            "--operation",
            "disk_usage",
            "--lxc-id",
            "135",
            "--request",
            str(request),
            "--sysroot",
            str(guest),
        ]
    )
    assert unpaired.returncode != 0
    assert "together" in unpaired.stderr

    old_with_mountinfo = _run_cli(
        [
            "--operation",
            "probe",
            "--lxc-id",
            "135",
            "--request",
            str(request),
            "--mountinfo",
            str(info),
        ]
    )
    assert old_with_mountinfo.returncode != 0
    assert "--mountinfo" in old_with_mountinfo.stderr


def test_disk_usage_subprocess_wrapper_blocked_worker_is_bounded(
    tmp_path: Path,
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/a.log", b"a" * 1000)
    info = _disk_mountinfo(tmp_path, guest)
    request = tmp_path / "disk-request.json"
    request.write_text(json.dumps(_disk_request(timeout_seconds=0.2)))
    wrapper = tmp_path / "blocked-disk-wrapper.py"
    wrapper.write_text(
        "import sys, time\n"
        "from proxmox_fleet import housekeeping_io as hio\n"
        "\n"
        "def block(**_kwargs):\n"
        "    while True:\n"
        "        time.sleep(1.0)\n"
        "\n"
        "hio._disk_worker_main = block\n"
        "sys.exit(hio.main(sys.argv[1:]))\n"
    )
    repo_root = Path(hio.__file__).resolve().parents[1]
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(repo_root) + (os.pathsep + existing if existing else "")
    )
    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            str(wrapper),
            "--operation",
            "disk_usage",
            "--lxc-id",
            "135",
            "--request",
            str(request),
            "--sysroot",
            str(guest),
            "--mountinfo",
            str(info),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 3.0
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "unavailable"
    assert payload["directories"] == []
    assert payload["reason"]


def test_disk_usage_excludes_a_same_device_file_bind_missing_from_the_table(
    tmp_path: Path, monkeypatch
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/var/log", 0o755)
    app = _write(guest, "/var/log/app.log", b"a" * 4000)
    mounted = _write(guest, "/var/log/mounted.log", b"m" * 12000)
    info = _disk_mountinfo(tmp_path, guest)
    real_mount_id = hio._fd_mount_id

    def fake_mount_id(fd: int):
        observed = real_mount_id(fd)
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return observed
        if target.rstrip("/").endswith("/var/log/mounted.log"):
            return (observed or 0) + 1
        return observed

    monkeypatch.setattr(hio, "_fd_mount_id", fake_mount_id)

    result = _disk_call(tmp_path, guest, mountinfo=info)

    assert result["status"] == "complete", result
    assert result["excluded_mounts"] == 1
    rows = _rows(result)
    assert rows["/var/log"]["allocated_bytes"] == _alloc(app)
    assert rows["/var/log"]["direct_bytes"] == _alloc(app)
    assert _alloc(mounted) > 0


def test_disk_usage_uses_a_fresh_worker_used_denominator(
    tmp_path: Path, monkeypatch
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/var/log/a.log", b"a" * 1000)
    monkeypatch.setattr(hio, "_fs_used_bytes", lambda path: 424242)

    result = _disk_call(tmp_path, guest)

    assert result["status"] == "complete", result
    assert result["root_used_bytes"] == 424242


def test_disk_usage_dispatch_without_pct_is_unavailable_and_never_walks(
    tmp_path: Path, monkeypatch
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_disk_request()))
    args = hio._parse_args(
        [
            "--operation",
            "disk_usage",
            "--lxc-id",
            "135",
            "--request",
            str(request_path),
        ]
    )
    walked = []
    guest_calls = []
    monkeypatch.setattr(hio, "_pct_path", lambda: None)
    monkeypatch.setattr(
        hio, "disk_usage", lambda *a, **k: walked.append((a, k))
    )
    monkeypatch.setattr(
        hio, "_pct_guest_call", lambda *a, **k: guest_calls.append((a, k))
    )

    result = hio.dispatch(args, _disk_request())

    assert result["status"] == "unavailable"
    assert result["directories"] == []
    assert walked == []
    assert guest_calls == []


def test_disk_usage_dispatch_stopped_guest_is_unavailable_without_a_start(
    tmp_path: Path, monkeypatch
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_disk_request()))
    args = hio._parse_args(
        [
            "--operation",
            "disk_usage",
            "--lxc-id",
            "135",
            "--request",
            str(request_path),
        ]
    )
    guest_calls = []
    monkeypatch.setattr(hio, "_pct_path", lambda: "/usr/bin/pct")
    monkeypatch.setattr(
        hio,
        "_pct_guest_state",
        lambda *a, **k: {
            "name": "guest",
            "os_type": "debian",
            "is_running": False,
            "is_template": False,
        },
    )
    monkeypatch.setattr(
        hio, "_pct_guest_call", lambda *a, **k: guest_calls.append((a, k))
    )

    result = hio.dispatch(args, _disk_request())

    assert result["status"] == "unavailable"
    assert "not running" in result["reason"]
    assert guest_calls == []


# --------------------------------------------------------------------------- #
# logging_acl (descriptor-anchored ACL repair)
# --------------------------------------------------------------------------- #

_REAL_ACL_UID = 424242


def _real_acl_guest(tmp_path: Path) -> Path:
    """A fixture sysroot whose ``alloy`` identity maps to a private numeric UID.

    The fixture carries absolute symlinks to the host ACL tools so the helper
    exercises real ``setfacl``/``getfacl`` behaviour on owned temporary files;
    no host account, permission or package is changed.
    """
    setfacl = shutil.which("setfacl")
    getfacl = shutil.which("getfacl")
    if setfacl is None or getfacl is None:
        pytest.skip("the host lacks setfacl/getfacl")
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(
        guest,
        "/etc/passwd",
        "root:x:0:0:root:/root:/bin/bash\n"
        f"alloy:x:{_REAL_ACL_UID}:{_REAL_ACL_UID}::/var/lib/alloy:/usr/sbin/nologin\n",
    )
    _mkdir(guest, "/usr/bin", 0o755)
    os.symlink(setfacl, guest / "usr/bin/setfacl")
    os.symlink(getfacl, guest / "usr/bin/getfacl")
    probe = _write(guest, "/acl-probe", "probe\n", 0o600)
    support = subprocess.run(
        [setfacl, "-m", f"u:{_REAL_ACL_UID}:r", "--", str(probe)],
        capture_output=True,
        check=False,
    )
    probe.unlink()
    if support.returncode != 0:
        pytest.skip("the temporary filesystem does not support POSIX ACLs")
    return guest


def _guest_path(guest: Path, path: str) -> Path:
    return guest / path.lstrip("/")


def _acl_entries(path: Path) -> list[str]:
    lines = []
    for line in subprocess.run(
        ["getfacl", "-n", "-p", "--", str(path)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines():
        if line.startswith("getfacl:"):
            continue
        lines.append(line)
    return lines


def _acl_perms(lines: list[str], uid: int = _REAL_ACL_UID, *, default: bool = False):
    prefix = ("default:" if default else "") + f"user:{uid}:"
    for line in lines:
        clean = line.split("#", 1)[0].strip()
        if clean.startswith(prefix):
            return clean[len(prefix) :]
    return None


def _has_uid(lines: list[str], uid: int = _REAL_ACL_UID) -> bool:
    return any(f":{uid}:" in line.split("#", 1)[0] for line in lines)


def test_logging_acl_repairs_real_descriptor_scoped_access(tmp_path: Path) -> None:
    guest = _real_acl_guest(tmp_path)
    _mkdir(guest, "/var/log/demo", 0o755)
    _write(guest, "/var/log/demo/a.log", "a\n")
    _mkdir(guest, "/var/log/demo/sub", 0o755)
    _write(guest, "/var/log/demo/sub/b.log", "b\n", 0o600)
    _mkdir(guest, "/var/log/demo/.fleet-housekeeping-quarantine", 0o755)
    _write(
        guest,
        "/var/log/demo/.fleet-housekeeping-quarantine/blob",
        "quarantine\n",
        0o600,
    )
    _mkdir(guest, "/opt/uptime-kuma/data", 0o700)
    _write(guest, "/opt/uptime-kuma/data/error.log", "error\n", 0o600)
    _write(guest, "/opt/uptime-kuma/data/kuma.db", "database\n", 0o600)

    request = {
        "roots": ["/var/log/demo"],
        "exact_files": [
            "/opt/uptime-kuma/data/error.log",
            "/opt/uptime-kuma/data/absent.log",
        ],
    }
    facts = hio.logging_acl(request, sysroot=str(guest))

    assert facts["status"] == "complete", facts
    assert facts["changed"] is True
    assert facts["repaired_roots"] == 1
    assert facts["repaired_files"] == 1
    assert facts["skipped_missing"] == 1
    assert facts["error_count"] == 0
    assert 0 < len(facts["reason"]) <= 200

    # Ancestors gain traverse-only access, never read or default inheritance.
    assert _acl_perms(_acl_entries(_guest_path(guest, "/var"))) == "--x"
    assert _acl_perms(_acl_entries(_guest_path(guest, "/var/log"))) == "--x"
    root_lines = _acl_entries(_guest_path(guest, "/var/log/demo"))
    assert _acl_perms(root_lines) == "r-x"
    assert _acl_perms(root_lines, default=True) == "r-x"
    sub_lines = _acl_entries(_guest_path(guest, "/var/log/demo/sub"))
    assert _acl_perms(sub_lines) == "r-x"
    assert _acl_perms(sub_lines, default=True) == "r-x"
    assert _acl_perms(_acl_entries(_guest_path(guest, "/var/log/demo/a.log"))) == "r--"
    assert (
        _acl_perms(_acl_entries(_guest_path(guest, "/var/log/demo/sub/b.log")))
        == "r--"
    )
    # The quarantine tree is pruned from the recursive grant.
    assert not _has_uid(
        _acl_entries(_guest_path(guest, "/var/log/demo/.fleet-housekeeping-quarantine"))
    )
    assert not _has_uid(
        _acl_entries(
            _guest_path(
                guest,
                "/var/log/demo/.fleet-housekeeping-quarantine/blob",
            )
        )
    )

    # The exact file parent is traverse-only, never recursively/read-granted,
    # and the sibling database is untouched.
    data_lines = _acl_entries(_guest_path(guest, "/opt/uptime-kuma/data"))
    assert _acl_perms(data_lines) == "--x"
    assert _acl_perms(data_lines, default=True) is None
    assert (
        _acl_perms(_acl_entries(_guest_path(guest, "/opt/uptime-kuma/data/error.log")))
        == "r--"
    )
    assert not _has_uid(
        _acl_entries(_guest_path(guest, "/opt/uptime-kuma/data/kuma.db"))
    )
    # The kernel ACL xattrs themselves show the grant on the log file and no
    # named-user xattr on the untouched sibling database.
    assert os.getxattr(
        _guest_path(guest, "/var/log/demo/a.log"), "system.posix_acl_access"
    )
    with pytest.raises(OSError):
        os.getxattr(
            _guest_path(guest, "/opt/uptime-kuma/data/kuma.db"),
            "system.posix_acl_access",
        )

    again = hio.logging_acl(request, sysroot=str(guest))
    assert again["status"] == "complete"
    assert again["changed"] is False
    assert again["repaired_roots"] == 1
    assert again["repaired_files"] == 1
    assert again["skipped_missing"] == 1


def test_logging_acl_refuses_swapped_symlinks_without_widening(tmp_path: Path) -> None:
    guest = _real_acl_guest(tmp_path)
    _mkdir(guest, "/opt/app/data", 0o755)
    database = _write(guest, "/opt/app/data/kuma.db", "database\n", 0o600)
    _mkdir(guest, "/var/log/demo", 0o755)
    _write(guest, "/var/log/demo/a.log", "a\n")

    # The classic probe-then-swap attack: the declared exact file is now a
    # symlink to an application database outside the audited log scope.
    os.symlink(database, _guest_path(guest, "/opt/app/data/error.log"))
    swapped = hio.logging_acl(
        {"roots": [], "exact_files": ["/opt/app/data/error.log"]},
        sysroot=str(guest),
    )
    assert swapped["status"] == "failed"
    assert swapped["error_count"] >= 1
    assert swapped["repaired_files"] == 0
    assert not _has_uid(_acl_entries(database))
    data_lines = _acl_entries(_guest_path(guest, "/opt/app/data"))
    assert _acl_perms(data_lines) == "--x"
    assert _acl_perms(data_lines, default=True) is None

    # A symlink appearing inside a dedicated root is skipped, never followed.
    os.unlink(_guest_path(guest, "/opt/app/data/error.log"))
    os.symlink(database, _guest_path(guest, "/var/log/demo/link.log"))
    tree = hio.logging_acl(
        {"roots": ["/var/log/demo"], "exact_files": []}, sysroot=str(guest)
    )
    assert tree["status"] == "complete", tree
    assert not _has_uid(_acl_entries(database))
    assert _acl_perms(_acl_entries(_guest_path(guest, "/var/log/demo/a.log"))) == "r--"

    # An unsafe ancestor symlink fails closed before reaching a target.
    os.symlink(
        _guest_path(guest, "/opt/app/data"),
        _guest_path(guest, "/opt/app/data-link"),
    )
    unsafe = hio.logging_acl(
        {"roots": [], "exact_files": ["/opt/app/data-link/kuma.db"]},
        sysroot=str(guest),
    )
    assert unsafe["status"] == "failed"
    assert unsafe["error_count"] >= 1
    assert not _has_uid(_acl_entries(database))


def test_logging_acl_skips_mountpoints_and_file_mounts(tmp_path: Path) -> None:
    guest = _real_acl_guest(tmp_path)
    _mkdir(guest, "/var/log/demo", 0o755)
    _write(guest, "/var/log/demo/live.log", "live\n")
    _mkdir(guest, "/var/log/demo/mounted", 0o755)
    _write(guest, "/var/log/demo/mounted/inside.log", "mounted\n")
    _write(guest, "/var/log/demo/bindfile.log", "bind file\n")
    _mkdir(guest, "/proc/self", 0o755)
    _write(
        guest,
        "/proc/self/mountinfo",
        "1 0 0:1 / / rw - tmpfs tmpfs rw\n"
        "2 1 0:2 / /var/log/demo/mounted rw - tmpfs tmpfs rw\n"
        "3 1 0:3 / /var/log/demo/bindfile.log rw - tmpfs tmpfs rw\n",
    )

    facts = hio.logging_acl(
        {"roots": ["/var/log/demo"], "exact_files": []}, sysroot=str(guest)
    )

    assert facts["status"] == "complete", facts
    assert _acl_perms(_acl_entries(_guest_path(guest, "/var/log/demo/live.log"))) == "r--"
    assert not _has_uid(
        _acl_entries(_guest_path(guest, "/var/log/demo/mounted"))
    )
    assert not _has_uid(
        _acl_entries(_guest_path(guest, "/var/log/demo/mounted/inside.log"))
    )
    assert not _has_uid(
        _acl_entries(_guest_path(guest, "/var/log/demo/bindfile.log"))
    )


def test_logging_acl_requires_tools_and_a_known_collector(tmp_path: Path) -> None:
    if shutil.which("getfacl") is None:
        pytest.skip("the host lacks getfacl")
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/etc/passwd", "root:x:0:0:root:/root:/bin/bash\n")
    _mkdir(guest, "/var/log/demo", 0o755)
    _write(guest, "/var/log/demo/a.log", "a\n")
    no_user = hio.logging_acl(
        {"roots": ["/var/log/demo"], "exact_files": []}, sysroot=str(guest)
    )
    assert no_user["status"] == "failed"
    assert "alloy" in no_user["reason"]

    _write(
        guest,
        "/etc/passwd",
        "root:x:0:0:root:/root:/bin/bash\n"
        f"alloy:x:{_REAL_ACL_UID}:{_REAL_ACL_UID}::/var/lib/alloy:/usr/sbin/nologin\n",
    )
    no_tools = hio.logging_acl(
        {"roots": ["/var/log/demo"], "exact_files": []}, sysroot=str(guest)
    )
    assert no_tools["status"] == "failed"
    assert "ACL tools" in no_tools["reason"]
    assert not _has_uid(_acl_entries(_guest_path(guest, "/var/log/demo/a.log")))

    with pytest.raises(hio.HelperError):
        hio.logging_acl(
            {"roots": ["/var/log/demo"], "exact_files": [], "extra": []},
            sysroot=str(guest),
        )
    with pytest.raises(hio.HelperError):
        hio.logging_acl(
            {"roots": ["/var/log/*"], "exact_files": []}, sysroot=str(guest)
        )
    with pytest.raises(hio.HelperError):
        hio.logging_acl({"roots": ["/var/log/demo"]}, sysroot=str(guest))


def test_probe_exact_file_distinguishes_denied_missing_and_unsafe_parents(
    tmp_path: Path,
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/etc/passwd", "alloy:x:991:991::/var/lib/alloy:/usr/sbin/nologin\n")
    alloy = hio._lookup_user(str(guest), "alloy")
    sysroot_dev = int(os.stat(guest).st_dev)
    _mkdir(guest, "/opt/denied", 0o700)
    _mkdir(guest, "/opt/open", 0o755)

    def probe(path: str, *, actor=alloy, mounts=None):
        return hio._probe_exact_file(
            path,
            sysroot=str(guest),
            alloy=actor,
            runner=hio._default_runner(str(guest)),
            sysroot_dev=sysroot_dev,
            mounts=set() if mounts is None else mounts,
        )

    denied = probe("/opt/denied/error.log")
    assert denied["status"] == "denied"
    assert denied["parent_traversable"] is False
    assert denied["exists"] is False

    quiet = probe("/opt/open/error.log")
    assert quiet["status"] == "quiet"
    assert quiet["parent_traversable"] is True

    missing = probe("/opt/absent/error.log")
    assert missing["status"] == "missing_root"
    assert missing["parent_traversable"] is False

    os.symlink(_guest_path(guest, "/opt/open"), _guest_path(guest, "/opt/linkdir"))
    unsafe = probe("/opt/linkdir/error.log")
    assert unsafe["status"] == "failed"

    unknown = probe("/opt/open/error.log", actor=None)
    assert unknown["status"] == "unavailable"

    _mkdir(guest, "/proc/self", 0o755)
    _write(
        guest,
        "/proc/self/mountinfo",
        "1 0 0:1 / / rw - tmpfs tmpfs rw\n"
        "2 1 0:2 / /opt/open rw - tmpfs tmpfs rw\n",
    )
    assert probe(
        "/opt/open/error.log", mounts=hio._read_mountpoints(str(guest))
    )["status"] == "failed"


def test_probe_source_exact_status_stays_repairable_for_denied_parent(
    tmp_path: Path,
) -> None:
    guest = tmp_path / "guest"
    _mkdir(guest, "/", 0o755)
    _write(guest, "/etc/passwd", "alloy:x:991:991::/var/lib/alloy:/usr/sbin/nologin\n")
    alloy = hio._lookup_user(str(guest), "alloy")
    sysroot_dev = int(os.stat(guest).st_dev)
    _mkdir(guest, "/opt/denied", 0o700)

    def layout(path: str, *, required: bool) -> dict:
        return hio._probe_source_layout(
            {
                "roots": [],
                "paths": [],
                "exact_files": [path],
                "exclude_paths": [],
                "required": required,
            },
            sysroot=str(guest),
            alloy=alloy,
            runner=hio._default_runner(str(guest)),
            clock=time.monotonic,
            deadline=time.monotonic() + 60.0,
            sysroot_dev=sysroot_dev,
            mounts=set(),
        )

    repairable = layout("/opt/denied/error.log", required=True)
    assert repairable["status"] == "denied"
    assert repairable["exact_files"]["/opt/denied/error.log"]["status"] == "denied"
    assert layout("/opt/absent/error.log", required=True)["status"] == "failed"
    assert (
        layout("/opt/absent/error.log", required=False)["status"] == "missing_root"
    )


def test_logging_acl_dispatch_fixture_sysroot_runs_the_operation(
    tmp_path: Path,
) -> None:
    guest = _real_acl_guest(tmp_path)
    _mkdir(guest, "/var/log/demo", 0o755)
    _write(guest, "/var/log/demo/a.log", "a\n")
    request = {"roots": ["/var/log/demo"], "exact_files": []}
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    args = hio._parse_args(
        [
            "--operation",
            "logging_acl",
            "--lxc-id",
            "135",
            "--sysroot",
            str(guest),
            "--request",
            str(request_path),
        ]
    )

    result = hio.dispatch(args, request)

    assert result["status"] == "complete"
    assert result["repaired_roots"] == 1


def test_logging_acl_dispatch_without_pct_fails_without_a_guest_call(
    tmp_path: Path, monkeypatch
) -> None:
    request = {"roots": ["/var/log/demo"], "exact_files": []}
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    args = hio._parse_args(
        [
            "--operation",
            "logging_acl",
            "--lxc-id",
            "135",
            "--request",
            str(request_path),
        ]
    )
    guest_calls = []
    monkeypatch.setattr(hio, "_pct_path", lambda: None)
    monkeypatch.setattr(
        hio, "_pct_guest_call", lambda *a, **k: guest_calls.append((a, k))
    )

    result = hio.dispatch(args, request)

    assert result["status"] == "failed"
    assert "pct" in result["reason"]
    assert guest_calls == []


def test_logging_acl_dispatch_stopped_guest_never_starts_or_repairs(
    tmp_path: Path, monkeypatch
) -> None:
    request = {"roots": ["/var/log/demo"], "exact_files": []}
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    args = hio._parse_args(
        [
            "--operation",
            "logging_acl",
            "--lxc-id",
            "135",
            "--request",
            str(request_path),
        ]
    )
    guest_calls = []
    monkeypatch.setattr(hio, "_pct_path", lambda: "/usr/bin/pct")
    monkeypatch.setattr(
        hio,
        "_pct_guest_state",
        lambda *a, **k: {
            "name": "guest",
            "os_type": "debian",
            "is_running": False,
            "is_template": False,
        },
    )
    monkeypatch.setattr(
        hio, "_pct_guest_call", lambda *a, **k: guest_calls.append((a, k))
    )

    result = hio.dispatch(args, request)

    assert result["status"] == "failed"
    assert "not running" in result["reason"]
    assert guest_calls == []
