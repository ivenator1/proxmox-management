"""Behavioral regression tests for install.sh's ``--update`` lifecycle.

These exercise the *actual* installer script (not a reimplementation, a mock of
its functions, or an assertion over its source text) inside a disposable
fixture: a throwaway git repo, a fake venv that reuses the real interpreter so
``resolve_setting`` genuinely resolves ``GlobalSettings`` from a fixture
``vars.yml``, an isolated ``systemctl`` shim, and a temporary unit directory.
The assertions observe the installed configuration after real git update
transitions; package installation and the real systemd daemon stay isolated.

The bug under test: ``install.sh`` executes as a whole file, so a ``--update``
that pulls a changed ``install.sh`` used to keep running the *old* function
bodies, ignoring the new logic until a second invocation. The fix re-execs the
freshly pulled installer with the original arguments, guarded against loops.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"

GIT = shutil.which("git")
REAL_ID = shutil.which("id") or "/usr/bin/id"
REAL_PYTHON = sys.executable

# The scoped targets the fixture vars.yml configures; the generated
# ExecStart must carry exactly these and must not widen to the whole fleet.
SCOPED_LIMIT = "default/120,default/123"

pytestmark = pytest.mark.skipif(GIT is None, reason="git is required")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _write_exe(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _install_text_without_limit_block() -> str:
    """Return install.sh with the scoped --limit unit logic removed.

    Simulates the *previous* installer behaviour (the observed symptom: the
    housekeeping service was written without ``--limit``). The re-exec fix is
    intentionally still present, because that is the version an operator would
    already have installed when a later ``--update`` pulls the new logic.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    needle = (
        '    if [ -n "$hk_limit" ]; then\n'
        '        hk_exec="$hk_exec --limit $hk_limit"\n'
        "    fi\n"
    )
    return text.replace(needle, "")


def _make_fixture(root: Path) -> Dict[str, Path]:
    """Create the disposable fixture: bare remote + work clone + shims."""
    work = root / "work"
    remote = root / "remote.git"
    unit_dir = root / "units"
    fixbin = root / "shim-bin"
    unit_dir.mkdir()
    fixbin.mkdir()

    subprocess.run(
        [GIT, "init", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    subprocess.run(
        [GIT, "clone", str(remote), str(work)],
        check=True, capture_output=True,
    )
    for key, val in (("user.email", "t@example.invalid"), ("user.name", "Tester")):
        subprocess.run([GIT, "-C", str(work), "config", key, val], check=True)
    # Empty remote: force the local branch name so `push origin main` is stable
    # regardless of the host's init.defaultBranch.
    _git(work, "checkout", "-B", "main")

    # Real interpreter so resolve_setting genuinely loads GlobalSettings,
    # but fake pip/ansible-galaxy so no package work happens.
    venv_bin = work / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_exe(venv_bin / "python", f'#!/bin/sh\nexec "{REAL_PYTHON}" "$@"\n')
    for tool in ("pip", "ansible-galaxy"):
        _write_exe(venv_bin / tool, "#!/bin/sh\nexit 0\n")

    # scoped settings the generated unit must reflect
    (work / "vars.yml").write_text(
        "housekeeping_timer_enabled: true\n"
        "housekeeping_timer_targets:\n"
        "  - default/120\n"
        "  - default/123\n"
        "housekeeping_loki_url: http://127.0.0.1:3100\n",
        encoding="utf-8",
    )

    # root check shim: report root so require_root passes in the fixture
    _write_exe(
        fixbin / "id",
        "#!/bin/sh\n"
        'if [ "${1:-}" = "-u" ]; then echo 0; exit 0; fi\n'
        f'exec "{REAL_ID}" "$@"\n',
    )

    systemctl = fixbin / "systemctl"
    _write_exe(
        systemctl,
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  is-enabled) echo enabled ;;\n'
        '  is-active) echo active ;;\n'
        "esac\n"
        "exit 0\n",
    )

    return {
        "work": work,
        "remote": remote,
        "unit_dir": unit_dir,
        "fixbin": fixbin,
    }


def _git(work: Path, *args: str) -> None:
    subprocess.run([GIT, "-C", str(work), *args], check=True, capture_output=True)


def _commit_install(fx: Dict[str, Path], text: str) -> None:
    work = fx["work"]
    _write_exe(work / "install.sh", text)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "installer snapshot")


def _run_update(fx: Dict[str, Path]) -> subprocess.CompletedProcess[str]:
    work = fx["work"]
    env = os.environ.copy()
    env.pop("FLEET_INSTALLER_REEXEC", None)
    env["PATH"] = f"{fx['fixbin']}{os.pathsep}{env['PATH']}"
    env["UNIT_DIR"] = str(fx["unit_dir"])
    env["SYSTEMCTL"] = str(fx["fixbin"] / "systemctl")
    # Make proxmox_fleet importable by the reused real interpreter.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(REPO_ROOT), env.get("PYTHONPATH", "")) if p
    )
    return subprocess.run(
        ["/bin/bash", str(work / "install.sh"), "--update"],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _unit(fx: Dict[str, Path], name: str) -> str:
    return (fx["unit_dir"] / name).read_text(encoding="utf-8")


def _exec_start(unit_text: str) -> Optional[str]:
    for line in unit_text.splitlines():
        if line.startswith("ExecStart="):
            return line
    return None




# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_update_reexecs_pulled_installer_logic_in_same_invocation(tmp_path: Path) -> None:
    """A --update that changes install.sh must run the NEW logic immediately."""
    fx = _make_fixture(tmp_path)
    work = fx["work"]
    old_text = _install_text_without_limit_block()

    # Pre-existing install written by the OLD unit logic: no scoped --limit.
    _commit_install(fx, old_text)
    _git(work, "push", "-q", "origin", "main")
    first = _run_update(fx)
    assert first.returncode == 0, first.stderr
    assert "--limit" not in (_exec_start(_unit(fx, "fleet-housekeeping.service")) or "")

    # Land the NEW installer (real install.sh with the scoped --limit logic) on
    # the remote, then rewind the local checkout to the old installer so the
    # pull is what actually changes install.sh — the real self-update sequence.
    _commit_install(fx, INSTALL_SH.read_text(encoding="utf-8"))
    _git(work, "push", "-q", "origin", "main")
    _git(work, "reset", "--hard", "HEAD~1")
    vars_before = (work / "vars.yml").read_bytes()

    second = _run_update(fx)
    assert second.returncode == 0, second.stderr

    # Consumer-visible result: the freshly pulled installer's logic produced the
    # scoped service config in this single invocation.
    installed = _exec_start(_unit(fx, "fleet-housekeeping.service"))
    assert installed is not None
    argv = shlex.split(installed.partition("=")[2])
    assert "--housekeeping-only" in argv
    assert set(argv[argv.index("--limit") + 1].split(",")) == set(SCOPED_LIMIT.split(","))

    # Operator config is preserved byte-for-byte by the update.
    assert (work / "vars.yml").read_bytes() == vars_before


def test_unchanged_installer_applies_changed_scope(tmp_path: Path) -> None:
    """Configuration drift is reconciled even when the installer does not change."""
    fx = _make_fixture(tmp_path)
    work = fx["work"]
    _commit_install(fx, INSTALL_SH.read_text(encoding="utf-8"))
    _git(work, "push", "-q", "origin", "main")
    initial = _run_update(fx)
    assert initial.returncode == 0, initial.stderr
    vars_path = work / "vars.yml"
    vars_path.write_text(
        vars_path.read_text(encoding="utf-8").replace("  - default/120\n", ""),
        encoding="utf-8",
    )

    proc = _run_update(fx)
    assert proc.returncode == 0, proc.stderr
    installed = _exec_start(_unit(fx, "fleet-housekeeping.service"))
    assert installed is not None
    argv = shlex.split(installed.partition("=")[2])
    assert set(argv[argv.index("--limit") + 1].split(",")) == {"default/123"}


def test_failed_pull_preserves_installed_configuration(tmp_path: Path) -> None:
    """A failed pull must not continue into unit/service work."""
    fx = _make_fixture(tmp_path)
    work = fx["work"]
    _commit_install(fx, INSTALL_SH.read_text(encoding="utf-8"))
    _git(work, "push", "-q", "origin", "main")

    # Establish a known-good installed state first.
    assert _run_update(fx).returncode == 0
    unit_before = _unit(fx, "fleet-housekeeping.service")

    # Break the remote so `git pull --ff-only` fails.
    _git(work, "remote", "set-url", "origin", str(tmp_path / "does-not-exist"))
    proc = _run_update(fx)

    assert proc.returncode != 0
    assert _unit(fx, "fleet-housekeeping.service") == unit_before
