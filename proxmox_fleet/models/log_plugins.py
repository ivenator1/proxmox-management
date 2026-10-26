"""Strict declarative schema for application-log plugin manifests.

These models are pure data validation.  A manifest can describe which units
must be running, which live log files may be read and (optionally) what share of
a guest's used root filesystem is normal for a directory.  It can never carry a
command, arbitrary HCL, a deletion directive, a URL or a path outside its own
declared/audited roots.

The schema is deliberately versioned (``schema_version: 1``) and forbids
unknown fields at every level.  A malformed file fails catalogue loading before
any guest mutation; there is no partial/silent acceptance.
"""
from __future__ import annotations

import math
import posixpath
import re
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Stable service, source, layout, component and guest-name identifiers.
ID_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"
_ID_RE = re.compile(ID_PATTERN)

#: Exact community-script identities (``UPDATE_SCRIPT_NAME`` or ct/NAME.sh).
_SCRIPT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Linux account names used by scoped user-unit probes and source ACLs.
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")

#: Alloy component label.  Only the legacy NPM/PBS sources may pin a name.
_COMPONENT_RE = re.compile(r"^fleet_[a-z0-9][a-z0-9_]{0,63}$")

#: Bounded systemd unit name with at most one instance glob.
_UNIT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.@:\\-]*(?:\*[A-Za-z0-9_.@:\\-]*)?"
    r"\.(?:service|socket|timer|target|path|mount|automount|slice|scope|swap|device)$"
)

#: Bare guest ids and full cluster/node/id override keys.
_GUEST_ID_RE = re.compile(r"^[1-9][0-9]{0,9}$")
_TOPOLOGY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: ``owner/source`` or ``owner/dependency/source``.
_SOURCE_KEY_RE = re.compile(
    rf"^{ID_PATTERN[1:-1]}(?:/{ID_PATTERN[1:-1]}){{1,2}}$"
)
#: ``service`` or ``owner/dependency``.
_LAYOUT_KEY_RE = re.compile(rf"^{ID_PATTERN[1:-1]}(?:/{ID_PATTERN[1:-1]})?$")

#: Components owned by the shared renderer.  Kept local so this model module
#: does not import the Alloy/runner stack.
_RESERVED_COMPONENTS = frozenset({"fleet_file_pack"})

#: Broader-than-a-service roots must never become read roots.
# B108 exemptions below are rejected roots, not temporary-file I/O.
_GENERIC_DIRECTORY_ROOTS = frozenset(
    {
        "/",
        "/var",
        "/var/log",
        "/var/lib",
        "/var/cache",
        "/var/tmp",  # nosec B108
        "/opt",
        "/home",
        "/Users",
        "/root",
        "/tmp",  # nosec B108
        "/data",
        "/usr",
        "/srv",
        "/boot",
        "/etc",
        "/proc",
        "/sys",
        "/dev",
        "/run",
        "/mnt",
        "/media",
    }
)

#: Trees that never contain an audited live application log source.
_FORBIDDEN_TOP_LEVELS = ("/proc", "/sys", "/dev", "/etc", "/boot")

#: Credential material must never be declared as a log source, even below an
#: otherwise audited log root.
_CREDENTIAL_COMPONENTS = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        ".password-store",
        ".netrc",
        ".pgpass",
    }
)
_CREDENTIAL_BASENAMES = frozenset(
    {
        "authorized_keys",
        "known_hosts",
        "id_rsa",
        "id_ed25519",
        ".env",
        ".git-credentials",
    }
)

#: Exact legacy label/component tuples.  These are the only sources allowed to
#: deviate from the new convention (app = plugin id, log_kind = source id).
_LEGACY_SOURCES: Dict[str, Dict[str, Tuple[str, str, str]]] = {
    "nginxproxymanager": {
        "application": ("nginxproxymanager", "application", "fleet_npm"),
    },
    "proxmox-backup-server": {
        "tasks": ("proxmox-backup", "task", "fleet_pbs_tasks"),
        "api": ("proxmox-backup", "api", "fleet_pbs_api"),
    },
}

_MAX_PATH_BYTES = 4096
_MAX_DISK_PREFIXES = 32
_MAX_DISK_PREFIX_DEPTH = 16
_MAX_DISK_PREFIX_BYTES = 512


# --------------------------------------------------------------------------- #
# Path validation helpers
# --------------------------------------------------------------------------- #


def _check_common_path(value: object, *, field: str, allow_glob: bool) -> str:
    """Validate one normalized absolute Linux path/pattern.

    Roots/exact files are literal paths; source globs additionally allow ``*``,
    ``?`` and one non-nested ``[...]`` bracket expression.  Braces are always
    rejected because exclusion unions are built from them by the renderer.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value:
        raise ValueError(f"{field} must not be empty")
    if len(value.encode("utf-8")) > _MAX_PATH_BYTES:
        raise ValueError(f"{field} is too long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field} must not contain control characters")
    if "{" in value or "}" in value:
        raise ValueError(f"{field} must not contain brace characters")
    if "\\" in value:
        raise ValueError(f"{field} must not contain backslashes")
    if "://" in value:
        raise ValueError(f"{field} must not be a URI")
    if not value.startswith("/") or value.startswith("//"):
        raise ValueError(f"{field} must be an absolute normalized path")
    if value != posixpath.normpath(value):
        raise ValueError(f"{field} must be normalized (no '.', '..', '//' or trailing slash)")
    if not allow_glob:
        if any(ch in value for ch in "*?["):
            raise ValueError(f"{field} must not contain glob characters")
    else:
        opened = False
        for ch in value:
            if ch == "[":
                if opened:
                    raise ValueError(f"{field} must not contain nested glob brackets")
                opened = True
            elif ch == "]":
                if not opened:
                    raise ValueError(f"{field} has an unmatched ']'")
                opened = False
        if opened:
            raise ValueError(f"{field} has an unmatched '['")
    return value


def _check_credential_path(value: str, *, field: str) -> None:
    components = [part.casefold() for part in value.split("/") if part]
    for component in components:
        if component in _CREDENTIAL_COMPONENTS:
            raise ValueError(f"{field} must not reference a credential path")
    if components and components[-1] in _CREDENTIAL_BASENAMES:
        raise ValueError(f"{field} must not reference a credential path")
    for index, component in enumerate(components[:-1]):
        if component == ".config" and components[index + 1] == "qbittorrent":
            raise ValueError(f"{field} must not reference qBittorrent credentials/session data")


def _check_source_tree(value: str, *, field: str) -> None:
    for prefix in _FORBIDDEN_TOP_LEVELS:
        if value == prefix or value.startswith(prefix + "/"):
            raise ValueError(f"{field} must not reference {prefix}")
    if value == "/root/.hermes" or value.startswith("/root/.hermes/"):
        raise ValueError(f"{field} must not reference the alternate-user Hermes home")
    _check_credential_path(value, field=field)


def normalize_directory_root(value: object, *, field: str = "root") -> str:
    """Validate a dedicated, audited log directory root."""
    path = _check_common_path(value, field=field, allow_glob=False)
    if path in _GENERIC_DIRECTORY_ROOTS:
        raise ValueError(f"{field} {path!r} is too broad for a log source")
    parts = path.split("/")
    if len(parts) == 3 and parts[1] in {"home", "Users"}:
        raise ValueError(f"{field} {path!r} must not be a user's home directory")
    _check_source_tree(path, field=field)
    return path


def normalize_exact_file(value: object, *, field: str = "exact_file") -> str:
    """Validate an individually audited exact log file path."""
    path = _check_common_path(value, field=field, allow_glob=False)
    if path == "/":
        raise ValueError(f"{field} must not be the filesystem root")
    _check_source_tree(path, field=field)
    return path


def normalize_source_pattern(value: object, *, field: str = "path") -> str:
    """Validate a live log glob beneath an audited root / exact file scope."""
    path = _check_common_path(value, field=field, allow_glob=True)
    if path == "/":
        raise ValueError(f"{field} must not be the filesystem root")
    _check_source_tree(path, field=field)
    return path


def normalize_location_paths(values: Sequence[object]) -> Tuple[str, ...]:
    """Validate a locations-override list without requiring catalogue scope."""
    seen: List[str] = []
    for raw in values:
        path = normalize_source_pattern(raw, field="locations entry")
        if path in seen:
            raise ValueError(f"duplicate locations entry {path!r}")
        seen.append(path)
    return tuple(seen)


def literal_prefix(pattern: str) -> str:
    """Return the literal prefix before the first glob metacharacter."""
    indexes = [index for ch in "*?[" if (index := pattern.find(ch)) >= 0]
    return pattern[: min(indexes)] if indexes else pattern


def pattern_beneath(pattern: str, root: str) -> bool:
    """Whether a declared pattern is strictly beneath an audited directory."""
    return literal_prefix(pattern).startswith(root + "/")


def patterns_overlap(first: str, second: str) -> bool:
    """Conservative literal-prefix ownership overlap (no glob intersection).

    Two literal (globless) paths are distinct concrete files unless identical;
    a glob and a literal path (or two globs) overlap when either literal prefix
    begins with the other.
    """
    first_glob = any(ch in first for ch in "*?[")
    second_glob = any(ch in second for ch in "*?[")
    if not first_glob and not second_glob:
        return first == second
    one = literal_prefix(first)
    two = literal_prefix(second)
    if not first_glob:
        return first.startswith(two)
    if not second_glob:
        return second.startswith(one)
    return one.startswith(two) or two.startswith(one)


def _assert_disjoint(patterns: Sequence[Tuple[str, str]]) -> None:
    """Reject two patterns in one ownership scope whose literal prefixes overlap."""
    for index, (first_label, first) in enumerate(patterns):
        for second_label, second in patterns[index + 1 :]:
            if patterns_overlap(first, second):
                raise ValueError(
                    f"overlapping source patterns {first_label!r} and {second_label!r} "
                    "must be consolidated into one disjoint pattern"
                )


def _reject_duplicates(values: Sequence[str], *, field: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {field} entry {value!r}")
        seen.add(value)


def path_depth(path: str) -> int:
    """Number of non-root path components (``/var/log`` is depth 2)."""
    return len([part for part in path.split("/") if part])


# --------------------------------------------------------------------------- #
# Unit evidence
# --------------------------------------------------------------------------- #


class UnitEvidence(BaseModel):
    """One systemd unit (or bounded instance glob) inspected for a service."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str
    scope: Literal["system", "user"] = "system"
    user: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value or len(value) > 256:
            raise ValueError("unit name must be a nonempty bounded string")
        if value.count("*") > 1:
            raise ValueError("unit name may contain at most one instance glob")
        if not _UNIT_RE.match(value):
            raise ValueError(f"invalid systemd unit name {value!r}")
        return value

    @field_validator("user")
    @classmethod
    def _validate_user(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _USER_RE.match(value):
            raise ValueError("user must be a bounded Linux account name")
        return value

    @model_validator(mode="after")
    def _scope_requires_user(self) -> "UnitEvidence":
        if self.scope == "user":
            if not self.user:
                raise ValueError("user-scope unit evidence requires an explicit user")
        elif self.user is not None:
            raise ValueError("system-scope unit evidence must not set user")
        return self

    @property
    def key(self) -> str:
        """Stable fact key: ``system:name`` or ``user:user:name``."""
        if self.scope == "user":
            return f"user:{self.user}:{self.name}"
        return f"system:{self.name}"


# --------------------------------------------------------------------------- #
# Source / layout / plugin manifests
# --------------------------------------------------------------------------- #


class MultilineOptions(BaseModel):
    """Bounded Alloy multiline stage options (validated for real before deploy)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    firstline: str
    max_lines: int = Field(default=128, ge=1, le=1024)
    max_wait_seconds: int = Field(default=3, ge=1, le=10)

    @field_validator("firstline")
    @classmethod
    def _validate_firstline(cls, value: str) -> str:
        if not value or len(value) > 256:
            raise ValueError("multiline firstline must be 1..256 characters")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("multiline firstline must not contain control characters")
        return value


class LogSource(BaseModel):
    """One live file-source reader within a layout."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str
    component: Optional[str] = None
    app: str
    log_kind: str
    allowed_roots: List[str] = Field(default_factory=list)
    alternate_roots: List[str] = Field(default_factory=list)
    exact_files: List[str] = Field(default_factory=list)
    paths: List[str] = Field(default_factory=list)
    exclude_paths: List[str] = Field(default_factory=list)
    required: bool = True
    multiline: Optional[MultilineOptions] = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError(f"invalid source id {value!r}")
        return value

    @field_validator("app", "log_kind")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError(f"invalid bounded log label {value!r}")
        return value

    @field_validator("component")
    @classmethod
    def _validate_component(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not _COMPONENT_RE.match(value):
            raise ValueError(f"invalid Alloy component name {value!r}")
        if value in _RESERVED_COMPONENTS:
            raise ValueError(f"component name {value!r} is reserved by the fleet renderer")
        return value

    @field_validator("allowed_roots", "alternate_roots")
    @classmethod
    def _validate_roots(cls, value: List[str]) -> List[str]:
        normalized = [normalize_directory_root(item, field="root") for item in value]
        _reject_duplicates(normalized, field="root")
        return normalized

    @field_validator("exact_files")
    @classmethod
    def _validate_exact_files(cls, value: List[str]) -> List[str]:
        normalized = [normalize_exact_file(item) for item in value]
        _reject_duplicates(normalized, field="exact file")
        return normalized

    @field_validator("paths", "exclude_paths")
    @classmethod
    def _validate_patterns(cls, value: List[str]) -> List[str]:
        normalized = [normalize_source_pattern(item) for item in value]
        _reject_duplicates(normalized, field="path pattern")
        return normalized

    @model_validator(mode="after")
    def _validate_scope(self) -> "LogSource":
        if not self.allowed_roots and not self.exact_files:
            raise ValueError("a file source requires at least one allowed root or exact file")
        if not self.paths and not self.exact_files:
            raise ValueError("a file source requires at least one path or exact file")
        for pattern in self.paths:
            if pattern not in self.exact_files and not any(
                pattern_beneath(pattern, root) for root in self.allowed_roots
            ):
                raise ValueError(
                    f"path {pattern!r} must be beneath an allowed root or equal an exact file"
                )
        for pattern in self.exclude_paths:
            if not any(
                pattern_beneath(pattern, root)
                for root in (*self.allowed_roots, *self.alternate_roots)
            ):
                raise ValueError(
                    f"exclusion {pattern!r} must be beneath an allowed or alternate root"
                )
        owners: List[Tuple[str, str]] = []
        for path in self.paths:
            owners.append((f"{self.id}:path", path))
        for path in self.exact_files:
            owners.append((f"{self.id}:exact", path))
        _assert_disjoint(owners)
        return self


class LogLayout(BaseModel):
    """One verified service layout, gated by explicit unit predicates."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str
    service_user: Optional[str] = None
    required_units: List[UnitEvidence]
    running_units_any: List[UnitEvidence] = Field(default_factory=list)
    sources: List[LogSource]

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError(f"invalid layout id {value!r}")
        return value

    @field_validator("service_user")
    @classmethod
    def _validate_service_user(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _USER_RE.match(value):
            raise ValueError("service_user must be a bounded Linux account name")
        return value

    @field_validator("required_units", "running_units_any")
    @classmethod
    def _validate_units(cls, value: List[UnitEvidence]) -> List[UnitEvidence]:
        seen = set()
        for unit in value:
            if unit.key in seen:
                raise ValueError(f"duplicate unit evidence {unit.key!r}")
            seen.add(unit.key)
        return value

    @field_validator("sources")
    @classmethod
    def _validate_sources(cls, value: List[LogSource]) -> List[LogSource]:
        seen = set()
        for source in value:
            if source.id in seen:
                raise ValueError(f"duplicate source id {source.id!r} in layout")
            seen.add(source.id)
        patterns: List[Tuple[str, str]] = []
        for source in value:
            for path in source.paths:
                patterns.append((f"{source.id}:path", path))
            for path in source.exact_files:
                patterns.append((f"{source.id}:exact", path))
        _assert_disjoint(patterns)
        return value

    @model_validator(mode="after")
    def _nonvacuous_when_source_less(self) -> "LogLayout":
        if not self.sources and not self.required_units and not self.running_units_any:
            raise ValueError(
                "a source-less layout must declare required_units or running_units_any"
            )
        return self

    @property
    def has_primary_predicate(self) -> bool:
        return bool(self.required_units or self.running_units_any)


class DiskUsagePolicy(BaseModel):
    """Optional per-service disk-ratio expectations (diagnostic metadata only)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    default_max_share_percent: Optional[float] = None
    min_directory_mb: Optional[int] = None
    path_max_share_percent: Dict[str, float] = Field(default_factory=dict)

    @field_validator("default_max_share_percent", mode="before")
    @classmethod
    def _validate_default_ratio(cls, value: object) -> object:
        if value is None:
            return None
        return _ratio(value, "default_max_share_percent")

    @field_validator("min_directory_mb", mode="before")
    @classmethod
    def _validate_floor(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("min_directory_mb must be a nonnegative integer")
        if value < 0:
            raise ValueError("min_directory_mb must be nonnegative")
        return value

    @field_validator("path_max_share_percent", mode="before")
    @classmethod
    def _validate_path_caps(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("path_max_share_percent must be a mapping")
        capped: Dict[str, float] = {}
        for raw_path, raw_ratio in value.items():
            path = _normalize_disk_prefix(raw_path)
            if path in capped:
                raise ValueError(f"duplicate disk-policy prefix {path!r}")
            capped[path] = _ratio(raw_ratio, f"path_max_share_percent[{path!r}]")
        if len(capped) > _MAX_DISK_PREFIXES:
            raise ValueError(f"at most {_MAX_DISK_PREFIXES} disk-policy prefixes are allowed")
        return capped

    def expected_share_percent(self, path: str) -> Optional[float]:
        """Longest-prefix expected cap for ``path`` (``/usr`` applies below it)."""
        normalized = posixpath.normpath(path)
        best: Optional[str] = None
        for prefix in self.path_max_share_percent:
            if normalized == prefix or normalized.startswith(prefix + "/"):
                if best is None or len(prefix) > len(best):
                    best = prefix
        return None if best is None else self.path_max_share_percent[best]


def _ratio(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number in (0, 100]")
    number = float(value)
    if not math.isfinite(number) or not (0.0 < number <= 100.0):
        raise ValueError(f"{field} must be a finite number in (0, 100]")
    return number


def _normalize_disk_prefix(value: object) -> str:
    path = _check_common_path(value, field="disk-policy prefix", allow_glob=False)
    if path == "/":
        raise ValueError("disk-policy prefix must not be the filesystem root")
    depth = path_depth(path)
    if depth < 1 or depth > _MAX_DISK_PREFIX_DEPTH:
        raise ValueError(
            f"disk-policy prefix depth must be 1..{_MAX_DISK_PREFIX_DEPTH} ({path!r})"
        )
    if len(path.encode("utf-8")) > _MAX_DISK_PREFIX_BYTES:
        raise ValueError("disk-policy prefix must be at most 512 UTF-8 bytes")
    return path


class LogPlugin(BaseModel):
    """One versioned service manifest (app or reusable dependency)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1]
    id: str
    display_name: str
    kind: Literal["app", "dependency"] = "app"
    community_script_ids: List[str]
    journal_units: List[UnitEvidence]
    includes: List[str] = Field(default_factory=list)
    layouts: List[LogLayout]
    disk_usage: Optional[DiskUsagePolicy] = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_schema_version(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int) or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError(f"invalid plugin id {value!r}")
        return value

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str) -> str:
        if not value or len(value) > 120:
            raise ValueError("display_name must be a nonempty bounded string")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("display_name must not contain control characters")
        return value

    @field_validator("community_script_ids")
    @classmethod
    def _validate_script_ids(cls, value: List[str]) -> List[str]:
        seen = set()
        for script_id in value:
            if not _SCRIPT_ID_RE.match(script_id):
                raise ValueError(f"invalid community-script id {script_id!r}")
            if script_id in seen:
                raise ValueError(f"duplicate community-script id {script_id!r}")
            seen.add(script_id)
        return value

    @field_validator("journal_units")
    @classmethod
    def _validate_journal_units(cls, value: List[UnitEvidence]) -> List[UnitEvidence]:
        seen = set()
        for unit in value:
            if unit.key in seen:
                raise ValueError(f"duplicate journal unit evidence {unit.key!r}")
            seen.add(unit.key)
        return value

    @field_validator("includes")
    @classmethod
    def _validate_includes(cls, value: List[str]) -> List[str]:
        seen = set()
        for include in value:
            if not _ID_RE.match(include):
                raise ValueError(f"invalid include service id {include!r}")
            if include in seen:
                raise ValueError(f"duplicate include service id {include!r}")
            seen.add(include)
        return value

    @field_validator("layouts")
    @classmethod
    def _validate_layouts(cls, value: List[LogLayout]) -> List[LogLayout]:
        if not value:
            raise ValueError("layouts must be a nonempty list")
        seen_layouts = set()
        for layout in value:
            if layout.id in seen_layouts:
                raise ValueError(f"duplicate layout id {layout.id!r}")
            seen_layouts.add(layout.id)
        return value

    @model_validator(mode="after")
    def _validate_kind_and_sources(self) -> "LogPlugin":
        if self.kind == "dependency":
            if self.community_script_ids:
                raise ValueError("dependency manifests must not declare community_script_ids")
            for layout in self.layouts:
                if not layout.has_primary_predicate:
                    raise ValueError(
                        "every dependency layout must declare a primary unit predicate"
                    )
        else:
            if not self.includes:
                for layout in self.layouts:
                    if not layout.has_primary_predicate:
                        raise ValueError(
                            "an app without includes must declare a primary unit "
                            "predicate in every layout"
                        )

        for layout in self.layouts:
            fixed_components = set()
            for source in layout.sources:
                legacy = _LEGACY_SOURCES.get(self.id, {}).get(source.id)
                if legacy is not None:
                    expected_app, expected_kind, expected_component = legacy
                    if (
                        source.app != expected_app
                        or source.log_kind != expected_kind
                        or source.component != expected_component
                    ):
                        raise ValueError(
                            f"legacy source {self.id}/{source.id} must preserve "
                            f"app={expected_app!r}, log_kind={expected_kind!r}, "
                            f"component={expected_component!r}"
                        )
                elif source.app != self.id or source.log_kind != source.id:
                    raise ValueError(
                        f"source {self.id}/{source.id} must use app={self.id!r} "
                        f"and log_kind={source.id!r}"
                    )
                if self.kind == "dependency" and source.component is not None:
                    raise ValueError("dependency sources must not pin a component name")
                if source.component is not None:
                    if source.component in fixed_components:
                        raise ValueError(f"duplicate component name {source.component!r}")
                    fixed_components.add(source.component)
        return self


# --------------------------------------------------------------------------- #
# Guest overrides
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GuestLogOverrideKey:
    """A syntactically valid full ``cluster/node/id`` or bare numeric guest key."""

    cluster: Optional[str]
    node: Optional[str]
    guest_id: str

    @property
    def is_bare(self) -> bool:
        return self.cluster is None

    @property
    def qualified(self) -> Optional[str]:
        if self.cluster is None or self.node is None:
            return None
        return f"{self.cluster}/{self.node}/{self.guest_id}"

    @classmethod
    def parse(cls, raw: object) -> "GuestLogOverrideKey":
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ValueError("guest override key must be a string or integer")
        value = str(raw)
        if not value:
            raise ValueError("guest override key must not be empty")
        parts = value.split("/")
        if len(parts) == 1:
            if not _GUEST_ID_RE.match(parts[0]):
                raise ValueError(
                    f"bare guest override key {value!r} must be a canonical positive numeric id"
                )
            return cls(cluster=None, node=None, guest_id=parts[0])
        if len(parts) != 3:
            raise ValueError(
                f"guest override key {value!r} must be 'cluster/node/id' or a bare numeric id"
            )
        cluster, node, guest_id = parts
        for label, segment in (("cluster", cluster), ("node", node)):
            if not _TOPOLOGY_NAME_RE.match(segment):
                raise ValueError(f"invalid {label} in guest override key {value!r}")
        if not _GUEST_ID_RE.match(guest_id):
            raise ValueError(f"invalid guest id in guest override key {value!r}")
        return cls(cluster=cluster, node=node, guest_id=guest_id)


class GuestLogOverride(BaseModel):
    """Per-guest log-plugin selection override (strict; unknown fields fail)."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    mode: Literal["auto", "replace", "extend", "disabled"] = "auto"
    plugins: List[str] = Field(default_factory=list)
    disable_plugins: List[str] = Field(default_factory=list)
    disable_sources: List[str] = Field(default_factory=list)
    locations: Dict[str, List[str]] = Field(default_factory=dict)
    layouts: Dict[str, str] = Field(default_factory=dict)

    @field_validator("plugins", "disable_plugins")
    @classmethod
    def _validate_plugin_ids(cls, value: List[str]) -> List[str]:
        seen = set()
        for plugin_id in value:
            if not _ID_RE.match(plugin_id):
                raise ValueError(f"invalid plugin/service id {plugin_id!r}")
            if plugin_id in seen:
                raise ValueError(f"duplicate plugin id {plugin_id!r}")
            seen.add(plugin_id)
        return value

    @field_validator("disable_sources")
    @classmethod
    def _validate_source_keys(cls, value: List[str]) -> List[str]:
        seen = set()
        for key in value:
            if not _SOURCE_KEY_RE.match(key):
                raise ValueError(
                    f"invalid source key {key!r}; expected owner/source or owner/dependency/source"
                )
            if key in seen:
                raise ValueError(f"duplicate disabled source key {key!r}")
            seen.add(key)
        return value

    @field_validator("locations", mode="before")
    @classmethod
    def _validate_locations(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("locations must be a mapping of source key to path list")
        normalized: Dict[str, List[str]] = {}
        for raw_key, raw_paths in value.items():
            key = str(raw_key)
            if not _SOURCE_KEY_RE.match(key):
                raise ValueError(
                    f"invalid locations source key {key!r}; "
                    "expected owner/source or owner/dependency/source"
                )
            if not isinstance(raw_paths, list):
                raise ValueError(f"locations[{key!r}] must be a list")
            normalized[key] = list(normalize_location_paths(raw_paths))
        return normalized

    @field_validator("layouts", mode="before")
    @classmethod
    def _validate_layout_overrides(cls, value: object) -> object:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("layouts must be a mapping of service key to layout id")
        normalized: Dict[str, str] = {}
        for raw_key, raw_layout in value.items():
            key = str(raw_key)
            layout = str(raw_layout)
            if not _LAYOUT_KEY_RE.match(key):
                raise ValueError(
                    f"invalid layout override key {key!r}; expected service or owner/dependency"
                )
            if not _ID_RE.match(layout):
                raise ValueError(f"invalid layout id {layout!r}")
            normalized[key] = layout
        return normalized

    @model_validator(mode="after")
    def _validate_mode(self) -> "GuestLogOverride":
        if self.mode == "auto" and self.plugins:
            raise ValueError("mode 'auto' must not carry explicit plugins")
        extras_present = bool(
            self.plugins
            or self.disable_plugins
            or self.disable_sources
            or self.locations
            or self.layouts
        )
        if self.mode == "disabled" and extras_present:
            raise ValueError("mode 'disabled' must not carry plugin/source/layout additions")
        overlap = set(self.plugins) & set(self.disable_plugins)
        if overlap:
            raise ValueError(
                "plugins and disable_plugins overlap: " + ", ".join(sorted(overlap))
            )
        conflict = set(self.disable_sources) & set(self.locations)
        if conflict:
            raise ValueError(
                "disable_sources and locations overlap: " + ", ".join(sorted(conflict))
            )
        return self
