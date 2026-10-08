"""Native journald/logrotate mechanics for the log-housekeeping feature.

This module owns the *native configuration mechanics* the manager drives; it
makes no retention, health or deletion-policy decisions:

* the exact bytes of the fleet-owned journald retention drop-in and the
  commands that write it and restart journald (drift only), plus the journal
  rotate/vacuum commands scoped to the same bounds — never an unlink of
  ``.journal`` files;
* a fail-closed parser for NPM's packaged
  ``/etc/logrotate.d/nginx-proxy-manager`` rule that identifies independent NPM
  stanzas without ever mixing their writer mechanisms, the managed-policy
  builder, the unrelated-bytes-preserving remainder and the atomic cutover
  commands;
* the bounded, symlink-safe owned-file read/write programs and their command
  builders, including the one-shot original-policy backup.

Loki health, sample ages, delivery acknowledgements, SQLite checkpoints and
file-deletion authorization stay in ``housekeeping_retention``; this module only
emits fixed native commands and parses native policy.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import shlex
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from proxmox_fleet import housekeeping_io as _io
from proxmox_fleet.housekeeping_io import NPM_LOG_ROOT
from proxmox_fleet.models.settings import GlobalSettings

__all__ = [
    "JOURNALD_DROPIN",
    "NPM_LOGROTATE_ORIGINAL",
    "NPM_LOGROTATE_DIR",
    "NPM_LOGROTATE_MANAGED",
    "NPM_LOGROTATE_BACKUP",
    "NPM_LOGROTATE_STATE",
    "journald_policy_content",
    "journald_policy_commands",
    "journal_vacuum_commands",
    "LogrotateBlock",
    "LogrotateParse",
    "parse_logrotate",
    "preserved_mechanism_lines",
    "build_npm_managed_logrotate",
    "npm_remaining_content",
    "npm_cutover_commands",
    "npm_logrotate_command",
    "read_file_command",
    "write_file_command",
]

# --------------------------------------------------------------------------- #
# Fixed owned locations
# --------------------------------------------------------------------------- #

#: Fleet-owned journald retention drop-in.
JOURNALD_DROPIN = _io.POLICY_FILES["journald"]

#: NPM's packaged global logrotate rule (the source of the cutover).
NPM_LOGROTATE_ORIGINAL = _io.NPM_NATIVE_LOGROTATE
#: Manager-owned hourly NPM rotation policy and its dedicated state file.
NPM_LOGROTATE_MANAGED = _io.POLICY_FILES["npm_logrotate"]
NPM_LOGROTATE_DIR = posixpath.dirname(NPM_LOGROTATE_MANAGED)
NPM_LOGROTATE_BACKUP = f"{NPM_LOGROTATE_DIR}/npm-logrotate.conf.orig"
NPM_LOGROTATE_STATE = f"{NPM_LOGROTATE_DIR}/npm-logrotate.state"


#: Fixed owned policy files this module may write or replace: the monitored
#: ``housekeeping_io.POLICY_FILES`` (journald, alloy env, managed NPM policy)
#: plus NPM's packaged rule that the cutover rewrites in place.  No arbitrary
#: path is ever a ``kind="policy"`` target.
_OWNED_POLICY_FILES = frozenset(_io.POLICY_FILES.values()) | {_io.NPM_NATIVE_LOGROTATE}

#: logrotate directives the manager owns; everything else in the original NPM
#: stanza (postrotate/create/su/…) is preserved as the existing mechanism.
_LOGROTATE_GOVERNED = frozenset(
    {
        "hourly",
        "daily",
        "weekly",
        "monthly",
        "yearly",
        "rotate",
        "maxage",
        "size",
        "minsize",
        "maxsize",
        "compress",
        "nocompress",
        "delaycompress",
        "nodelaycompress",
        "dateext",
        "nodateext",
        "dateformat",
        "dateyesterday",
        "datehourago",
        "notifempty",
        "ifempty",
        "missingok",
        "nomissingok",
    }
)

_NPM_MANAGED_COMMON = (
    "compress",
    "delaycompress",
    "dateext",
    "dateformat -%Y%m%dT%H%M%S",
    "rotate -1",
    "notifempty",
    "missingok",
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# journald retention
# --------------------------------------------------------------------------- #


def journald_policy_content(settings: GlobalSettings) -> str:
    """Exact bytes of the fleet-owned journald retention drop-in."""
    return (
        "[Journal]\n"
        f"SystemMaxUse={int(settings.housekeeping_journal_max_mb)}M\n"
        f"SystemKeepFree={int(settings.housekeeping_journal_keep_free_mb)}M\n"
        f"MaxRetentionSec={int(settings.housekeeping_local_retention_hours)}h\n"
    )


def journald_policy_commands(settings: GlobalSettings) -> List[str]:
    """Write the drop-in and restart journald (drift only)."""
    return [
        "install -d -m 0755 -- /etc/systemd/journald.conf.d",
        write_file_command(JOURNALD_DROPIN, journald_policy_content(settings)),
        f"chmod 0644 -- {JOURNALD_DROPIN}",
        "systemctl restart systemd-journald",
    ]


def journal_vacuum_commands(settings: GlobalSettings) -> List[str]:
    """Rotate and vacuum archived journals inside the same native bounds."""
    return [
        "journalctl --rotate",
        "journalctl --vacuum-size={0}M --vacuum-time={1}h".format(
            int(settings.housekeeping_journal_max_mb),
            int(settings.housekeeping_local_retention_hours),
        ),
    ]


# --------------------------------------------------------------------------- #
# Bounded owned-file read/write
# --------------------------------------------------------------------------- #


_WRITE_PROGRAM = """import hashlib, os, stat, sys, uuid
path, content, kind, expected = sys.argv[1:5]
parts = path.split('/')
if parts[0] or any(part in ('', '.', '..') for part in parts[1:]):
    raise ValueError('invalid owned path')
directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
temporary = None
try:
    for part in parts[1:-1]:
        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        os.close(directory)
        directory = child
    name = parts[-1]
    try:
        previous = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        previous = None
    if previous is not None and not stat.S_ISREG(previous.st_mode):
        raise ValueError('owned target is not a regular file')
    if kind != 'marker':
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise ValueError('policy directory is not protected')
    if kind == 'backup' and previous is not None:
        sys.exit(0)
    if expected:
        existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(existing, 'rb') as handle:
            if hashlib.sha256(handle.read(1048577)).hexdigest() != expected:
                raise ValueError('native policy changed during cutover')
    if kind == 'marker':
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=directory)
    else:
        temporary = '.fleet-write-' + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(content.encode('utf-8'))
        handle.flush()
        os.fchmod(handle.fileno(), 0o640 if kind == 'marker' else (stat.S_IMODE(previous.st_mode) if previous else 0o644))
        if previous is not None:
            os.fchown(handle.fileno(), previous.st_uid, previous.st_gid)
        os.fsync(handle.fileno())
    if temporary:
        try:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        signature = lambda value: None if value is None else (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if signature(previous) != signature(current):
            raise ValueError('owned policy changed during write')
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
    os.fsync(directory)
finally:
    if temporary:
        os.unlink(temporary, dir_fd=directory)
    os.close(directory)
"""
_READ_PROGRAM = (
    "import json,sys\n"
    "try:\n"
    "    with open(sys.argv[1], 'rb') as handle:\n"
    "        raw = handle.read(1048577)\n"
    "    if len(raw) > 1048576:\n"
    "        raise ValueError('native policy exceeds 1 MiB')\n"
    "    sys.stdout.write(json.dumps({'content': raw.decode('utf-8')}))\n"
    "except FileNotFoundError:\n"
    "    sys.stdout.write(json.dumps({'missing': True}))\n"
)


def write_file_command(
    path: str, content: str, *, kind: str = "policy", expected: str = ""
) -> str:
    if kind == "policy" and path not in _OWNED_POLICY_FILES:
        raise ValueError(f"not an owned policy file: {path!r}")
    return (
        f"python3 -c {shlex.quote(_WRITE_PROGRAM)} "
        + " ".join(shlex.quote(value) for value in (path, content, kind, expected))
    )


def read_file_command(path: str) -> str:
    return f"python3 -c {shlex.quote(_READ_PROGRAM)} {shlex.quote(path)}"


# --------------------------------------------------------------------------- #
# NPM logrotate parser / managed policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LogrotateBlock:
    header: Tuple[str, ...]
    body: Tuple[str, ...]
    span: Tuple[int, int]


@dataclass(frozen=True)
class LogrotateParse:
    blocks: Tuple[LogrotateBlock, ...]
    npm_blocks: Tuple[LogrotateBlock, ...]
    ambiguous: Optional[str]


def _strip_comment(line: str) -> str:
    index = line.find("#")
    return line if index < 0 else line[:index]


def _header_tokens(text: str) -> Tuple[str, ...]:
    return tuple(shlex.split(text, comments=True))


def _scan_logrotate_blocks(text: str) -> List[LogrotateBlock]:
    lines = text.splitlines(keepends=True)
    offsets: List[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    blocks: List[LogrotateBlock] = []
    index = 0
    while index < len(lines):
        stripped = _strip_comment(lines[index])
        if "{" not in stripped:
            index += 1
            continue
        header_text, remainder = stripped.split("{", 1)
        header = _header_tokens(header_text)
        if not header or remainder.strip():
            raise ValueError("unsupported inline or multiline logrotate stanza")
        body: List[str] = []
        cursor = index
        in_script = False
        closed = False
        while cursor + 1 < len(lines):
            cursor += 1
            inner = _strip_comment(lines[cursor]).strip()
            if not in_script and inner == "}":
                closed = True
                break
            if not in_script and ("{" in inner or "}" in inner):
                raise ValueError("unsupported nested logrotate stanza")
            body.append(lines[cursor].rstrip("\n"))
            if inner in ("postrotate", "prerotate", "firstaction", "lastaction", "preremove"):
                in_script = True
            elif inner == "endscript":
                in_script = False
        if not closed or in_script:
            raise ValueError("unterminated logrotate stanza or writer script")
        end = offsets[cursor] + len(lines[cursor])
        blocks.append(
            LogrotateBlock(header=header, body=tuple(body), span=(offsets[index], end))
        )
        index = cursor + 1
    return blocks


def parse_logrotate(text: str) -> LogrotateParse:
    """Identify independent NPM stanzas; never mix their writer mechanisms."""
    try:
        blocks = tuple(_scan_logrotate_blocks(text))
    except ValueError as exc:
        return LogrotateParse(blocks=(), npm_blocks=(), ambiguous=str(exc))
    npm_blocks: List[LogrotateBlock] = []
    for block in blocks:
        under = [token for token in block.header if token.startswith(NPM_LOG_ROOT + "/")]
        if not under:
            continue
        if len(under) != len(block.header) or any("/../" in token for token in under):
            return LogrotateParse(blocks=blocks, npm_blocks=(),
                ambiguous="a logrotate stanza mixes NPM paths with unrelated or traversal paths")
        npm_blocks.append(block)
    if not npm_blocks and NPM_LOG_ROOT in text:
        return LogrotateParse(blocks=blocks, npm_blocks=(),
            ambiguous="NPM paths are present but their stanzas could not be parsed safely")
    return LogrotateParse(blocks=blocks, npm_blocks=tuple(npm_blocks), ambiguous=None)


def preserved_mechanism_lines(body: Sequence[str]) -> List[str]:
    """Preserve writer scripts and ownership; change only rotation policy."""
    out: List[str] = []
    in_script = False
    for line in body:
        token = line.strip().split(None, 1)[0].lower() if line.strip() else ""
        if in_script or token not in _LOGROTATE_GOVERNED:
            out.append(line.rstrip("\n"))
        if token in ("postrotate", "prerotate", "firstaction", "lastaction", "preremove"):
            in_script = True
        elif token == "endscript":
            in_script = False
    return out


def build_npm_managed_logrotate(blocks: Sequence[LogrotateBlock]) -> str:
    """Keep native targets and each stanza's reopen/copytruncate mechanism."""
    lines: List[str] = []
    for block in blocks:
        header = " ".join(json.dumps(path) if any(ch.isspace() for ch in path) else path for path in block.header)
        lines.append(f"{header} {{")
        lines.extend(preserved_mechanism_lines(block.body))
        backend = all(posixpath.basename(path) == "backend.log" for path in block.header)
        lines.append("    size 10M" if backend else "    hourly")
        lines.extend(f"    {directive}" for directive in _NPM_MANAGED_COMMON)
        lines.extend(("}", ""))
    return "\n".join(lines).rstrip("\n") + "\n"


def npm_remaining_content(text: str, blocks: Sequence[LogrotateBlock]) -> str:
    """Remove only recognized NPM spans, preserving all unrelated bytes."""
    for block in sorted(blocks, key=lambda item: item.span[0], reverse=True):
        start, end = block.span
        text = text[:start] + text[end:]
    return text


def npm_cutover_commands(
    *, managed_content: str, remaining_content: Optional[str], original_content: str
) -> List[str]:
    """Save the original once; atomically replace only parsed NPM spans."""
    commands = [f"install -d -m 0755 -- {shlex.quote(NPM_LOGROTATE_DIR)}"]
    if remaining_content is not None:
        commands.append(write_file_command(NPM_LOGROTATE_BACKUP, original_content, kind="backup"))
    commands.append(write_file_command(NPM_LOGROTATE_MANAGED, managed_content))
    if remaining_content is not None:
        commands.append(write_file_command(
            NPM_LOGROTATE_ORIGINAL, remaining_content, expected=_sha256_text(original_content)
        ))
    return commands


def npm_logrotate_command() -> str:
    """Run the managed policy with a dedicated state file (no --force)."""
    return (
        f"logrotate --state {shlex.quote(NPM_LOGROTATE_STATE)} "
        f"{shlex.quote(NPM_LOGROTATE_MANAGED)}"
    )
