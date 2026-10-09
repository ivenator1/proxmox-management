"""Real-filesystem regressions for the step-2 housekeeping transport helper.

Every test drives ``proxmox_fleet.housekeeping_io`` against a temporary sysroot
tree (never a mocked filesystem), plus a deterministic command runner so tool
output is stable on any host.  No test requires root and none touches a real
guest.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from proxmox_fleet import housekeeping_io as hio

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
