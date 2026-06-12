"""SSH key setup for host enrollment: list/generate keys, push, test.

``push_key()`` is ssh-copy-id without the sshpass dependency: OpenSSH will run
``$SSH_ASKPASS`` to obtain a password when it has no tty
(``SSH_ASKPASS_REQUIRE=force``), so we point it at a throwaway 0700 script
that prints ``$FLEET_SSH_PW``. The password therefore lives only in the
subprocess environment — never on argv (visible in ``ps``), never in the
script body on disk, never logged, never persisted.

All subprocess calls are argv lists (no shell), all user-supplied fields are
regex-validated before any argv is built, and the runner is injectable for
tests.
"""
from __future__ import annotations

import os
import re
import subprocess  # nosec B404 - argv lists only, shell never used
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

# hostnames/IPs (incl. IPv6 colons) and usernames — no shell metacharacters.
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-]+$")
_USER_RE = re.compile(r"^[A-Za-z0-9._\-]+$")

DEFAULT_KEY = "~/.ssh/id_ed25519"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class SshSetupError(ValueError):
    """Invalid input for an SSH action."""


def _validated(host: str, user: str, port: Any) -> Tuple[str, str, int]:
    host, user = str(host).strip(), str(user).strip() or "root"
    if not _HOST_RE.match(host):
        raise SshSetupError(f"invalid host {host!r}")
    if not _USER_RE.match(user):
        raise SshSetupError(f"invalid user {user!r}")
    try:
        port_n = int(str(port).strip() or "22")
    except ValueError:
        raise SshSetupError(f"invalid port {port!r}")
    if not 0 < port_n < 65536:
        raise SshSetupError(f"invalid port {port_n}")
    return host, user, port_n


def list_public_keys() -> List[Dict[str, str]]:
    """Manager's ``~/.ssh/*.pub`` keys: path, type, key blob, comment."""
    out: List[Dict[str, str]] = []
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.is_dir():
        return out
    for pub in sorted(ssh_dir.glob("*.pub")):
        try:
            content = pub.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        parts = content.split(None, 2)
        if len(parts) < 2:
            continue
        out.append({
            "path": str(pub),
            "type": parts[0],
            "key": content,
            "comment": parts[2] if len(parts) > 2 else "",
        })
    return out


def generate_key(path: str = DEFAULT_KEY, *,
                 run: Runner = subprocess.run) -> Tuple[bool, str]:
    """``ssh-keygen -t ed25519`` at *path* (no passphrase — Ansible needs
    unattended logins). Refuses to overwrite an existing key."""
    key_path = Path(path).expanduser()
    if key_path.exists() or key_path.with_suffix(".pub").exists():
        raise SshSetupError(f"{key_path} already exists — refusing to overwrite")
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    proc = run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "fleet-manager",
         "-f", str(key_path)],
        capture_output=True, text=True, timeout=30,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


def push_key(host: str, user: str, password: str, *, port: Any = 22,
             pubkey_path: str = DEFAULT_KEY + ".pub",
             run: Runner = subprocess.run) -> Tuple[bool, str]:
    """ssh-copy-id with the password supplied via SSH_ASKPASS (see module
    docstring). Returns ``(ok, combined_output)``."""
    host, user, port_n = _validated(host, user, port)
    if not password:
        raise SshSetupError("password is required to push a key")
    pub = Path(pubkey_path).expanduser()
    if not pub.is_file():
        raise SshSetupError(f"public key {pub} not found — generate one first")

    fd, askpass = tempfile.mkstemp(prefix="fleet-askpass-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\nprintf \'%s\' "$FLEET_SSH_PW"\n')
        os.chmod(askpass, 0o700)
        env = dict(os.environ)
        env.update({
            "SSH_ASKPASS": askpass,
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": env.get("DISPLAY", ":0"),  # legacy openssh wants it set
            "FLEET_SSH_PW": password,
        })
        proc = run(
            ["ssh-copy-id", "-i", str(pub), "-p", str(port_n),
             "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=15",
             f"{user}@{host}"],
            capture_output=True, text=True, timeout=60, env=env,
            start_new_session=True,  # no controlling tty → askpass fires
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output
    finally:
        try:
            os.unlink(askpass)
        except OSError:
            pass


def test_key(host: str, user: str, *, port: Any = 22,
             key_path: str = DEFAULT_KEY,
             run: Runner = subprocess.run) -> Tuple[bool, str]:
    """``ssh -o BatchMode=yes … true`` — rc 0 means passwordless login works."""
    host, user, port_n = _validated(host, user, port)
    key = Path(key_path).expanduser()
    cmd = ["ssh", "-p", str(port_n), "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
    if key.is_file():
        cmd += ["-i", str(key)]
    cmd += [f"{user}@{host}", "true"]
    proc = run(cmd, capture_output=True, text=True, timeout=30)
    output = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0
    return ok, output or ("passwordless login OK" if ok else "")
