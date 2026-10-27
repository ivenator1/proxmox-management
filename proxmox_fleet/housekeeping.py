"""Manager-owned LXC logging, acknowledged archival and maintenance policy.

Python owns live logging preparation, acknowledged archival, gated retention
and fixed native cache profiles. Ansible transports only exact operations.
Recurring maintenance never installs packages or starts stopped guests.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

from proxmox_fleet.alloy import (
    AlloyProbe,
    DesiredAlloyConfig,
    parse_probe,
    reconcile_alloy,
    render_lxc_log_config,
)
from proxmox_fleet import housekeeping_native as native
from proxmox_fleet.cluster import matches_any
from proxmox_fleet.housekeeping_checkpoint import CheckpointStore, GuestKey
from proxmox_fleet.housekeeping_import import import_guest_logs
from proxmox_fleet.housekeeping_io import (
    POLICY_FILES,
    REQUIRED_BINARIES,
)
from proxmox_fleet.housekeeping_sources import SourceProvenance, logical_identity
from proxmox_fleet.log_plugins import (
    LogCatalogue,
    LogPluginError,
    LogSelection,
    ResolvedLogSelection,
    ResolvedLogSource,
    build_logging_request,
    select_log_plugins,
    validate_log_layout,
)
from proxmox_fleet.models.log_plugins import GuestLogOverride
from proxmox_fleet.models.state import HousekeepingSummary

if TYPE_CHECKING:  # avoid a runtime executor/flow cycle; used for typing only
    from proxmox_fleet.executor import Executor
    from proxmox_fleet.models.settings import GlobalSettings
    from proxmox_fleet.runner import PrimitiveResult


_ARCHIVE_LOCK = threading.Lock()

#: Fleet-owned Alloy journal-retention environment drop-in (systemd override).
#: The path is the same owned policy file the node helper hashes as
#: ``policy_sha256["alloy_env"]``, so the two can never drift apart.
FLEET_ALLOY_ENV_DROPIN = POLICY_FILES["alloy_env"]
FLEET_JOURNAL_MAX_AGE_VAR = "FLEET_JOURNAL_MAX_AGE"
#: The exact expression the base desired journal source must use for the
#: configured retention window to drive restart catch-up.  A custom journal
#: source that does not use it is reported for explicit migration instead of
#: being rewritten.
JOURNAL_ENV_EXPRESSION = f'sys.env("{FLEET_JOURNAL_MAX_AGE_VAR}")'

#: Full-evidence keys per profile.  A profile is only ever recognized (and thus
#: eligible for configuration/deletion) when every key is literally true; a
#: generic ``/data/logs`` directory in another application is never enough.
_EVIDENCE_KEYS: Dict[str, Tuple[str, ...]] = {
    "npm": ("npm.service", "openresty.service", "app_root", "log_root"),
    "pbs": (
        "proxmox-backup-manager",
        "proxmox-backup-proxy.service",
        "task_root",
        "api_root",
    ),
}

#: ACL tooling the helper needs both to repair and to verify Alloy access.
_ACL_TOOLS: Tuple[str, ...] = ("setfacl", "getfacl")

#: Guest OS IDs the feature is allowed to configure (Debian/apt-based).
_SUPPORTED_OS: frozenset = frozenset({"debian", "ubuntu"})

#: Taxonomy and identity the parser trusts; a record outside these sets is a
#: malformed fact stream, never something to act on.
_VALID_COMPRESSION = frozenset({"plain", "gzip", "zstd"})
_VALID_PROFILE = frozenset({"npm", "pbs"})
_VALID_LOG_KIND = frozenset({"application", "task", "api", "task_index"})

#: Required probe fact shape.  A truncated or malformed probe result must fail
#: closed rather than be read as "nothing there" and reported as success.
_REQUIRED_FACTS: Tuple[str, ...] = (
    "guest",
    "disk",
    "journal_bytes",
    "profiles",
    "files",
    "cache_paths",
    "busy_tools",
    "binaries",
    "log_access_ready",
    "log_access_detail",
    "policy_sha256",
    "profile_evidence",
    "logging",
)
_REQUIRED_GUEST_KEYS: Tuple[str, ...] = ("name", "os_type", "is_running", "is_template")
_REQUIRED_POLICY_KEYS: Tuple[str, ...] = ("journald", "alloy_env", "npm_logrotate")
_REQUIRED_FILE_KEYS: Tuple[str, ...] = (
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
)
_REQUIRED_DISK_KEYS: Tuple[str, ...] = ("total_bytes", "available_bytes", "used_percent")
_REQUIRED_ACCESS_KEYS: Tuple[str, ...] = ("alloy_user", "dirs", "files")

#: Status vocabularies the read-only helper may return.  They are validated
#: here so a truncated/garbled logging payload fails closed before any policy
#: module sees it; semantic layout choice remains in ``log_plugins``.
_VALID_LOG_STATUS: frozenset = frozenset(
    {"ok", "quiet", "missing_root", "denied", "unavailable", "failed", "partial"}
)
_VALID_UNIT_STATUS: frozenset = frozenset(
    {"active", "inactive", "missing", "unavailable"}
)

_URL_ATTR_RE = re.compile(r'\burl\s*=\s*"([^"\n]+)"')


class HousekeepingError(RuntimeError):
    """A read-only probe or maintenance precondition could not be satisfied."""


@dataclass
class HousekeepingResult:
    summary: Optional[HousekeepingSummary] = None
    changed: bool = False
    warnings: List[str] = field(default_factory=list)
    failed: bool = False
    effective_alloy: Optional[DesiredAlloyConfig] = None


@dataclass(frozen=True)
class HousekeepingProbe:
    """Typed view of the read-only ``lxc_housekeeping_probe`` facts.

    ``guest`` carries ``name``/``os_type``/``is_running``/``is_template``;
    ``binaries`` maps tool name -> resolved path or ``None``.  ``recognized`` is
    the profile set trusted only after full ``profile_evidence`` verification.
    ``logging`` is the raw catalogue evidence maps (always present; empty when
    no request was sent or the guest is stopped).
    """

    guest: Dict[str, Any]
    disk: Optional[Dict[str, Any]]
    journal_bytes: int
    profiles: Tuple[str, ...]
    recognized: Tuple[str, ...]
    files: Tuple[Dict[str, Any], ...]
    cache_paths: Dict[str, Any]
    busy_tools: Tuple[str, ...]
    binaries: Dict[str, Any]
    log_access_ready: bool
    log_access_detail: Dict[str, Any]
    policy_sha256: Dict[str, str]
    profile_evidence: Dict[str, Any]
    warnings: Tuple[str, ...] = ()
    logging: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_running(self) -> bool:
        return bool(self.guest.get("is_running"))

    @property
    def is_template(self) -> bool:
        return bool(self.guest.get("is_template"))

    @property
    def os_type(self) -> str:
        return str(self.guest.get("os_type") or "").strip().lower()

    @property
    def name(self) -> str:
        return str(self.guest.get("name") or "")


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _result_detail(result: PrimitiveResult) -> str:
    detail = str(result.stderr or result.stdout or "primitive failed").strip()
    return " ".join(detail.split())[-400:]


def _dedup(items: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _log_mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise HousekeepingError(f"housekeeping probe returned malformed {label} facts")
    return value


def _logging_request_unit_keys(entry: Any, *, label: str) -> Set[str]:
    service = _log_mapping(entry, label)
    layouts = service.get("layouts")
    if not isinstance(layouts, list) or not layouts:
        raise HousekeepingError(f"logging request {label}.layouts is malformed")
    keys: Set[str] = set()

    def collect(values: Any, field_label: str) -> None:
        if values is None:
            return
        if not isinstance(values, list):
            raise HousekeepingError(f"logging request {field_label} is malformed")
        for unit in values:
            if not isinstance(unit, dict):
                raise HousekeepingError(f"logging request {field_label} is malformed")
            key = unit.get("key")
            if not isinstance(key, str) or not key:
                raise HousekeepingError(
                    f"logging request {field_label} is missing a unit key"
                )
            keys.add(key)

    collect(service.get("journal_units"), f"{label}.journal_units")
    for layout in layouts:
        if not isinstance(layout, dict):
            raise HousekeepingError(f"logging request {label}.layouts is malformed")
        collect(layout.get("required_units"), f"{label}.required_units")
        collect(layout.get("running_units_any"), f"{label}.running_units_any")
    return keys


def _validate_unit_records(units: Dict[str, Any], label: str) -> None:
    for key, record in units.items():
        if not isinstance(key, str) or not key:
            raise HousekeepingError(f"housekeeping probe returned malformed {label}")
        if not isinstance(record, dict):
            raise HousekeepingError(f"housekeeping probe returned malformed {label}")
        status = record.get("status")
        if status not in _VALID_UNIT_STATUS:
            raise HousekeepingError(
                f"housekeeping probe returned an invalid unit status in {label}"
            )
        instances = record.get("instances", {})
        if not isinstance(instances, dict):
            raise HousekeepingError(f"housekeeping probe returned malformed {label}")
        for instance, instance_status in instances.items():
            if (
                not isinstance(instance, str)
                or not instance
                or "*" in instance
                or "/" in instance
            ):
                raise HousekeepingError(
                    f"housekeeping probe returned an invalid unit instance in {label}"
                )
            if instance_status not in _VALID_UNIT_STATUS:
                raise HousekeepingError(
                    f"housekeeping probe returned an invalid instance status in {label}"
                )
        if "*" in key and status == "active" and not instances:
            raise HousekeepingError(
                f"housekeeping probe claims an active unit glob without instances in {label}"
            )


def _validate_log_root_fact(record: Any, label: str) -> None:
    entry = _log_mapping(record, label)
    for key in ("exists", "is_dir", "is_symlink", "blocked_ancestor"):
        if not isinstance(entry.get(key), bool):
            raise HousekeepingError(f"housekeeping probe returned malformed {label}")
    device = entry.get("device")
    if device is not None and not _is_nonnegative_int(device):
        raise HousekeepingError(f"housekeeping probe returned malformed {label}")
    if entry["exists"] and device is None:
        raise HousekeepingError(
            f"housekeeping probe returned an existing root without a device in {label}"
        )
    if entry.get("status") not in _VALID_LOG_STATUS:
        raise HousekeepingError(
            f"housekeeping probe returned an invalid root status in {label}"
        )
    access = entry.get("access")
    if access is not None:
        access_map = _log_mapping(access, f"{label}.access")
        for key in ("traversable", "readable", "default_acl"):
            if not isinstance(access_map.get(key), bool):
                raise HousekeepingError(
                    f"housekeeping probe returned malformed {label}.access"
                )


def _validate_match_fact(record: Any, pattern: str, label: str) -> None:
    entry = _log_mapping(record, label)
    if entry.get("pattern") != pattern:
        raise HousekeepingError(
            f"housekeeping probe returned match evidence out of order in {label}"
        )
    if not _is_nonnegative_int(entry.get("matched")):
        raise HousekeepingError(f"housekeeping probe returned malformed {label}")
    if not _is_nonnegative_int(entry.get("skipped")):
        raise HousekeepingError(f"housekeeping probe returned malformed {label}")
    if not isinstance(entry.get("truncated"), bool):
        raise HousekeepingError(f"housekeeping probe returned malformed {label}")
    if entry.get("status") not in _VALID_LOG_STATUS:
        raise HousekeepingError(
            f"housekeeping probe returned an invalid match status in {label}"
        )
    files = entry.get("files")
    if not isinstance(files, list):
        raise HousekeepingError(f"housekeeping probe returned malformed {label}")
    if entry["matched"] < len(files):
        raise HousekeepingError(
            f"housekeeping probe returned inconsistent match counts in {label}"
        )
    for file_entry in files:
        item = _log_mapping(file_entry, f"{label}.files")
        if (
            not isinstance(item.get("path"), str)
            or not item["path"].startswith("/")
            or not _is_nonnegative_int(item.get("device"))
            or not _is_nonnegative_int(item.get("inode"))
        ):
            raise HousekeepingError(
                f"housekeeping probe returned malformed {label}.files"
            )
        for key in ("size", "mtime_ns"):
            value = item.get(key)
            if value is not None and not _is_nonnegative_int(value):
                raise HousekeepingError(
                    f"housekeeping probe returned malformed {label}.files"
                )
        if "accessible" in item and not isinstance(item["accessible"], bool):
            raise HousekeepingError(
                f"housekeeping probe returned malformed {label}.files"
            )


def _validate_exact_file_fact(record: Any, label: str) -> None:
    entry = _log_mapping(record, label)
    for key in ("exists", "is_symlink"):
        if not isinstance(entry.get(key), bool):
            raise HousekeepingError(f"housekeeping probe returned malformed {label}")
    for key in ("device", "inode", "size", "mtime_ns"):
        value = entry.get(key)
        if value is not None and not _is_nonnegative_int(value):
            raise HousekeepingError(f"housekeeping probe returned malformed {label}")
    for key in ("same_device", "parent_traversable", "is_file"):
        if key in entry and not isinstance(entry[key], bool):
            raise HousekeepingError(f"housekeeping probe returned malformed {label}")
    if entry.get("status") not in _VALID_LOG_STATUS:
        raise HousekeepingError(
            f"housekeeping probe returned an invalid exact-file status in {label}"
        )
    access = entry.get("access")
    if access is not None:
        access_map = _log_mapping(access, f"{label}.access")
        if not isinstance(access_map.get("readable"), bool):
            raise HousekeepingError(
                f"housekeeping probe returned malformed {label}.access"
            )


def _log_string_list(value: Any, label: str) -> List[str]:
    """Validate one request list as bounded strings, preserving order."""
    if not isinstance(value, list):
        raise HousekeepingError(f"logging request {label} is malformed")
    items: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise HousekeepingError(f"logging request {label} is malformed")
        items.append(item)
    return items


def _validate_source_layout_fact(fact: Any, descriptor: Any, label: str) -> None:
    entry = _log_mapping(fact, label)
    layout = _log_mapping(descriptor, f"request {label}")
    if not isinstance(entry.get("truncated"), bool):
        raise HousekeepingError(f"housekeeping probe returned malformed {label}")
    if entry.get("status") not in _VALID_LOG_STATUS:
        raise HousekeepingError(
            f"housekeeping probe returned an invalid source status in {label}"
        )
    roots = _log_string_list(layout.get("roots"), f"{label}.roots")
    paths = _log_string_list(layout.get("paths"), f"{label}.paths")
    exact_files = _log_string_list(
        layout.get("exact_files"), f"{label}.exact_files"
    )
    roots_fact = _log_mapping(entry.get("roots"), f"{label}.roots")
    if set(roots_fact) != set(roots):
        raise HousekeepingError(
            "logging root evidence does not match the requested roots"
        )
    for root in roots:
        _validate_log_root_fact(roots_fact[root], f"logging root {root!r}")
    matches = entry.get("matches")
    if not isinstance(matches, list) or len(matches) != len(paths):
        raise HousekeepingError(
            "logging match evidence does not match the requested paths"
        )
    for index, pattern in enumerate(paths):
        _validate_match_fact(
            matches[index], pattern, f"logging match {pattern!r}"
        )
    exact_fact = _log_mapping(entry.get("exact_files"), f"{label}.exact_files")
    if set(exact_fact) != set(exact_files):
        raise HousekeepingError(
            "logging exact-file evidence does not match the requested files"
        )
    for path in exact_files:
        _validate_exact_file_fact(
            exact_fact[path], f"logging exact file {path!r}"
        )


def _validate_logging_facts(
    logging: Any,
    request: Optional[Dict[str, Any]],
    *,
    expect_empty: bool = False,
) -> None:
    evidence = _log_mapping(logging, "logging")
    plugins = _log_mapping(evidence.get("plugins"), "logging plugins")
    sources = _log_mapping(evidence.get("sources"), "logging sources")
    if expect_empty:
        if plugins or sources:
            raise HousekeepingError(
                "housekeeping probe returned logging facts for a stopped guest"
            )
        return
    if request is None:
        if plugins or sources:
            raise HousekeepingError(
                "housekeeping probe returned unsolicited logging facts"
            )
        return
    expected = _log_mapping(request, "logging request")
    expected_plugins = _log_mapping(
        expected.get("plugins", {}), "logging request plugins"
    )
    expected_sources = _log_mapping(
        expected.get("sources", {}), "logging request sources"
    )
    if set(plugins) != set(expected_plugins):
        raise HousekeepingError(
            "logging plugin evidence does not match the request"
        )
    for owner_id, owner_request in expected_plugins.items():
        owner_fact = _log_mapping(plugins[owner_id], "logging plugin evidence")
        units = _log_mapping(owner_fact.get("units"), "logging units")
        expected_units = _logging_request_unit_keys(
            owner_request, label=f"plugin {owner_id!r}"
        )
        if set(units) != expected_units:
            raise HousekeepingError(
                "logging unit evidence does not match the request"
            )
        _validate_unit_records(units, f"logging units for {owner_id!r}")
        includes = _log_mapping(
            owner_fact.get("includes"), "logging includes"
        )
        expected_includes = owner_request.get("includes", {})
        if not isinstance(expected_includes, dict):
            raise HousekeepingError(
                f"logging request includes for {owner_id!r} are malformed"
            )
        if set(includes) != set(expected_includes):
            raise HousekeepingError(
                "logging include evidence does not match the request"
            )
        for dependency_id, dependency_request in expected_includes.items():
            include_fact = _log_mapping(
                includes[dependency_id], "logging include evidence"
            )
            include_units = _log_mapping(
                include_fact.get("units"), "logging include units"
            )
            expected_dependency_units = _logging_request_unit_keys(
                dependency_request,
                label=f"include {owner_id}/{dependency_id}",
            )
            if set(include_units) != expected_dependency_units:
                raise HousekeepingError(
                    "logging include unit evidence does not match the request"
                )
            _validate_unit_records(
                include_units,
                f"logging units for {owner_id}/{dependency_id}",
            )
    if set(sources) != set(expected_sources):
        raise HousekeepingError(
            "logging source evidence does not match the request"
        )
    for key, source_request in expected_sources.items():
        if not isinstance(source_request, dict):
            raise HousekeepingError(
                f"logging request source {key!r} is malformed"
            )
        layouts = source_request.get("layouts")
        if not isinstance(layouts, dict) or not layouts:
            raise HousekeepingError(
                f"logging request source {key!r} has no layouts"
            )
        source_fact = _log_mapping(sources[key], "logging source evidence")
        fact_layouts = _log_mapping(
            source_fact.get("layouts"), "logging source layouts"
        )
        if set(fact_layouts) != set(layouts):
            raise HousekeepingError(
                "logging layout evidence does not match the request"
            )
        for layout_id, descriptor in layouts.items():
            _validate_source_layout_fact(
                fact_layouts[layout_id],
                descriptor,
                f"logging source {key!r} layout {layout_id!r}",
            )


def recognized_profiles(facts: Dict[str, Any]) -> Tuple[Set[str], List[str]]:
    """Trust ``profiles`` only where the full ``profile_evidence`` matches.

    Returns ``(recognized, warnings)``.  A profile the probe declared but whose
    evidence is incomplete (or a profile the probe could not evidence at all)
    yields a warning and is never recognized, so partial evidence can never
    authorize configuration or deletion.
    """
    evidence = _as_dict(facts.get("profile_evidence"))
    declared = {str(p) for p in (facts.get("profiles") or [])}
    recognized: Set[str] = set()
    warnings: List[str] = []
    for profile, keys in _EVIDENCE_KEYS.items():
        item = _as_dict(evidence.get(profile))
        missing = [key for key in keys if item.get(key) is not True]
        if not missing:
            recognized.add(profile)
            continue
        if profile in declared or item.get("detected") is True or any(item.get(key) is True for key in keys):
            warnings.append(
                f"{profile}: incomplete profile evidence "
                f"({', '.join(missing)} not confirmed); skipping configuration and deletion"
            )
    for profile in sorted(declared - set(_EVIDENCE_KEYS)):
        warnings.append(f"{profile}: unsupported profile reported by the probe; ignoring")
    return recognized, warnings


def _require_complete_probe(
    facts: Dict[str, Any],
    *,
    logging_request: Optional[Dict[str, Any]] = None,
) -> None:
    """Fail closed on a truncated, missing or malformed probe fact stream.

    The runner has silently dropped oversized results before; an incomplete
    probe must never be read as emptiness and reported as success.  When a
    logging request was sent, every requested owner/include/layout/source/unit
    key must be present exactly once; the helper never fills gaps with guesses.
    """
    missing = [key for key in _REQUIRED_FACTS if key not in facts]
    if missing:
        raise HousekeepingError(
            "housekeeping probe returned incomplete facts (missing: " + ", ".join(missing) + ")"
        )
    guest = facts["guest"]
    if not isinstance(guest, dict):
        raise HousekeepingError("housekeeping probe returned malformed guest facts")
    missing = [key for key in _REQUIRED_GUEST_KEYS if key not in guest]
    if missing:
        raise HousekeepingError(
            "housekeeping probe returned incomplete guest facts (missing: " + ", ".join(missing) + ")"
        )
    if any(not isinstance(guest[key], bool) for key in ("is_running", "is_template")):
        raise HousekeepingError("housekeeping probe returned malformed guest state")
    for key in ("files", "profiles", "busy_tools"):
        if not isinstance(facts[key], list):
            raise HousekeepingError(f"housekeeping probe returned malformed {key} facts")
    for key in ("cache_paths", "binaries", "log_access_detail", "policy_sha256", "profile_evidence"):
        if not isinstance(facts[key], dict):
            raise HousekeepingError(f"housekeeping probe returned malformed {key} facts")
    if not isinstance(facts["log_access_ready"], bool):
        raise HousekeepingError("housekeeping probe returned malformed log_access_ready")
    if (
        not isinstance(facts["journal_bytes"], int)
        or isinstance(facts["journal_bytes"], bool)
        or facts["journal_bytes"] < 0
    ):
        raise HousekeepingError("housekeeping probe returned malformed journal_bytes")
    missing = [key for key in _REQUIRED_POLICY_KEYS if key not in facts["policy_sha256"]]
    if missing:
        raise HousekeepingError(
            "housekeeping probe returned incomplete policy hashes (missing: " + ", ".join(missing) + ")"
        )
    missing = [profile for profile in _EVIDENCE_KEYS if profile not in facts["profile_evidence"]]
    if missing:
        raise HousekeepingError(
            "housekeeping probe returned incomplete profile evidence (missing: " + ", ".join(missing) + ")"
        )
    running = bool(guest.get("is_running")) and not bool(guest.get("is_template"))
    _validate_logging_facts(
        facts["logging"], logging_request, expect_empty=not running
    )
    if running:
        disk = facts["disk"]
        if not isinstance(disk, dict) or any(key not in disk for key in _REQUIRED_DISK_KEYS):
            raise HousekeepingError("housekeeping probe returned incomplete disk facts")
        if any(name not in facts["binaries"] for name in REQUIRED_BINARIES):
            raise HousekeepingError("housekeeping probe returned incomplete binary facts")
        if any(key not in facts["log_access_detail"] for key in _REQUIRED_ACCESS_KEYS):
            raise HousekeepingError("housekeeping probe returned incomplete access facts")
    for entry in facts["files"]:
        if not isinstance(entry, dict) or any(key not in entry for key in _REQUIRED_FILE_KEYS):
            raise HousekeepingError("housekeeping probe returned a malformed file record")
        if (
            not isinstance(entry["path"], str)
            or not entry["path"].startswith("/")
            or not isinstance(entry["is_active"], bool)
            or any(
                not isinstance(entry[key], int) or isinstance(entry[key], bool) or entry[key] < 0
                for key in ("device", "inode", "size", "mtime_ns", "allocated_bytes")
            )
        ):
            raise HousekeepingError("housekeeping probe returned an invalid file identity")
        if (
            entry["compression"] not in _VALID_COMPRESSION
            or entry["profile"] not in _VALID_PROFILE
            or entry["log_kind"] not in _VALID_LOG_KIND
        ):
            raise HousekeepingError("housekeeping probe returned an invalid file taxonomy")


def probe_housekeeping(
    executor: "Executor",
    lxc_id: str,
    *,
    logging_request: Optional[Dict[str, Any]] = None,
) -> HousekeepingProbe:
    """Run the read-only probe and return its typed facts.

    ``logging_request`` is the optional manager-built catalogue descriptor.  It
    is transported verbatim; only the helper measures its requested unit/source
    keys.  Raises :class:`HousekeepingError` when the probe primitive itself
    fails or returns an incomplete/malformed fact stream, so callers never
    mistake a failed or truncated enumeration for emptiness.
    """
    result = executor.housekeeping_probe(lxc_id, logging_request=logging_request)
    if result.failed or result.rc != 0:
        raise HousekeepingError(f"housekeeping probe failed: {_result_detail(result)}")
    facts = _as_dict(result.facts)
    _require_complete_probe(facts, logging_request=logging_request)
    recognized, warnings = recognized_profiles(facts)
    guest = _as_dict(facts.get("guest"))
    binaries = _as_dict(facts.get("binaries"))
    return HousekeepingProbe(
        guest=guest,
        disk=_as_dict(facts.get("disk")) or None,
        journal_bytes=int(facts.get("journal_bytes") or 0),
        profiles=tuple(str(p) for p in (facts.get("profiles") or [])),
        recognized=tuple(sorted(recognized)),
        files=tuple(facts.get("files") or []),
        cache_paths=_as_dict(facts.get("cache_paths")),
        busy_tools=tuple(str(t) for t in (facts.get("busy_tools") or [])),
        binaries=binaries,
        log_access_ready=bool(facts.get("log_access_ready")),
        log_access_detail=_as_dict(facts.get("log_access_detail")),
        policy_sha256={
            str(k): str(v) for k, v in _as_dict(facts.get("policy_sha256")).items()
        },
        profile_evidence=_as_dict(facts.get("profile_evidence")),
        warnings=tuple(warnings),
        logging=_as_dict(facts.get("logging")),
    )


def journal_env_content(retention_hours: int) -> str:
    """Exact bytes of the fleet-owned Alloy journal-retention environment drop-in."""
    return (
        f"[Service]\n"
        f"Environment={FLEET_JOURNAL_MAX_AGE_VAR}={int(retention_hours)}h\n"
    )


def base_uses_journal_env(base: DesiredAlloyConfig) -> bool:
    """True when the base journal source reads the fleet retention variable."""
    return JOURNAL_ENV_EXPRESSION in base.content


def alloy_env_commands(retention_hours: int) -> List[str]:
    """Apply the owned environment drop-in through the guarded policy writer."""
    return [
        "install -d -m 0755 -- /etc/systemd/system/alloy.service.d",
        native.write_file_command(FLEET_ALLOY_ENV_DROPIN, journal_env_content(retention_hours)),
        "systemctl daemon-reload",
        "systemctl restart alloy",
    ]


def _log_acl_targets(
    sources: Sequence[ResolvedLogSource],
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Deduplicated manager targets for one descriptor-anchored ACL repair.

    Only fully validated catalogue descriptors may be passed here.  Dedicated
    roots and exact files are kept as separate arrays so the helper can apply
    recursive directory grants only to real log directories and read-only
    grants only to individually audited files.
    """
    roots = sorted({root for source in sources for root in source.roots})
    exact_files = sorted({path for source in sources for path in source.exact_files})
    return tuple(roots), tuple(exact_files)


_LOG_ACL_COUNT_KEYS: Tuple[str, ...] = (
    "repaired_roots",
    "repaired_files",
    "skipped_missing",
    "error_count",
)


def _log_acl_outcome(result: PrimitiveResult) -> Tuple[Optional[str], bool]:
    """Validate one logging-ACL helper result; any doubt blocks deployment.

    The helper is the only component allowed to mutate permissions, so a
    malformed or incomplete fact stream must never be treated as success.  The
    returned reason is already sanitized/bounded for warning records.
    """
    prefix = "failed to grant Alloy scoped log access"
    if result.failed or result.rc != 0:
        return f"{prefix}: {_result_detail(result)}", False
    facts = _as_dict(result.facts)
    if not isinstance(facts.get("reason"), str):
        return f"{prefix}: helper returned malformed reason fact", False
    reason = _sanitize_finding(facts["reason"]) or "helper reported no reason"
    if facts.get("status") != "complete":
        return f"{prefix}: {reason}", False
    changed = facts.get("changed")
    if not isinstance(changed, bool):
        return f"{prefix}: helper returned malformed changed fact", False
    for key in _LOG_ACL_COUNT_KEYS:
        if not _is_nonnegative_int(facts.get(key)):
            return f"{prefix}: helper returned malformed {key} fact", False
    if facts["error_count"]:
        return f"{prefix}: {facts['error_count']} target error(s); {reason}", False
    return None, changed


def guest_write_endpoint(content: str) -> Optional[str]:
    """First ``url = "..."`` value in the desired HCL (the Loki write endpoint)."""
    match = _URL_ATTR_RE.search(content)
    return match.group(1) if match else None


def _endpoint_key(url: str) -> Tuple[str, str, int, str]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, (parts.hostname or "").lower(), port, parts.path.rstrip("/")


def _render_effective(
    base: DesiredAlloyConfig,
    *,
    node: str,
    cluster: str,
    lxc_id: str,
    name: str,
    sources: Sequence[ResolvedLogSource],
    retention_hours: int,
) -> DesiredAlloyConfig:
    """Append live file sources for the verified selection.

    A source-free selection keeps the base config byte-for-byte, so a guest with
    no managed application logs — an intentional empty selection or an
    unresolved/unsupported identity — never gains a generated component.
    """
    if not sources:
        return base
    return render_lxc_log_config(
        base,
        node=node,
        cluster=cluster,
        lxc_id=lxc_id,
        name=name,
        sources=sources,
        retention_hours=retention_hours,
    )


def _sanitize_finding(text: str, *, limit: int = 240) -> str:
    """Bound and sanitize policy text before it reaches a warning record."""
    cleaned = " ".join(str(text).split()).replace("`", "'")
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3] + "..."
    return cleaned


def _alloy_state(executor: "Executor", lxc_id: str) -> AlloyProbe:
    result = executor.alloy_probe(lxc_id=lxc_id)
    if result.failed or result.rc != 0:
        raise HousekeepingError(f"Alloy probe failed: {_result_detail(result)}")
    return parse_probe(_as_dict(result.facts))


def _live_source_findings(resolved: ResolvedLogSelection) -> List[str]:
    """Findings worth persisting for a successful run.

    A quiet optional match is routine (for example NPM between requests), so it
    never becomes a fleet warning by itself; a genuinely inactive optional
    source, by contrast, stays explicit until it appears.
    """
    return [
        finding
        for finding in resolved.findings
        if "source is quiet, delivery unverified" not in finding
    ]


def _source_signature(source: ResolvedLogSource) -> Tuple[Any, ...]:
    return (
        source.owner_id,
        source.service_id,
        source.source_id,
        source.layout_id,
        source.kind,
        source.app,
        source.log_kind,
        source.component,
        source.required,
        source.multiline,
        source.paths,
        source.exclude_paths,
        source.roots,
        source.exact_files,
        source.service_user,
    )


def _resolution_drift(
    expected: ResolvedLogSelection,
    actual: ResolvedLogSelection,
    final_sources: Sequence[ResolvedLogSource],
) -> Optional[str]:
    """Detect guest/layout/source drift that invalidates verified evidence."""
    expected_services = {
        service.occurrence_key: (service.layout_id, service.kind)
        for service in expected.services
    }
    actual_services = {
        service.occurrence_key: (service.layout_id, service.kind)
        for service in actual.services
    }
    if expected_services != actual_services:
        return "selected service layout changed"
    expected_keys = {source.key for source in expected.sources}
    actual_sources = {source.key: source for source in actual.sources}
    if set(actual_sources) != expected_keys:
        return "selected source set changed"
    for source in final_sources:
        current = actual_sources.get(source.key)
        if current is None:
            return f"source {source.key} disappeared"
        if _source_signature(current) != _source_signature(source):
            return f"source {source.key} definition changed"
        if not current.renderable:
            return f"source {source.key} is {current.status}"
    return None


#: Legacy archive profiles and the exact default-complete owner/source shape
#: that may authorize their existing import/native/prune pipeline.
_LEGACY_ARCHIVE_SOURCES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("npm", "nginxproxymanager", ("application",)),
    ("pbs", "proxmox-backup-server", ("tasks", "api")),
)


def legacy_archive_profiles(
    selection: LogSelection, resolved: ResolvedLogSelection
) -> frozenset[str]:
    """Legacy archive eligibility implied by the immutable resolved selection.

    This is a further restriction, never an entitlement: ``npm`` requires the
    default complete enabled ``nginxproxymanager/application`` source and
    ``pbs`` both default ``proxmox-backup-server/tasks`` and ``/api`` sources.
    Any disabled, location-overridden, non-default-layout or otherwise remapped
    legacy source makes that entire profile ineligible.
    """
    if selection.status != "selected" or resolved.status != "selected":
        return frozenset()
    catalogue: LogCatalogue = selection.catalogue
    sources = {source.key: source for source in resolved.sources}
    eligible: Set[str] = set()
    for profile, owner, source_ids in _LEGACY_ARCHIVE_SOURCES:
        if selection.layouts.get(owner, "default") != "default":
            continue
        try:
            plugin = catalogue.plugin(owner)
        except LogPluginError:
            continue
        complete = True
        for source_id in source_ids:
            key = f"{owner}/{source_id}"
            source = sources.get(key)
            if (
                source is None
                or source.owner_id != owner
                or source.service_id != owner
                or source.layout_id != "default"
                or key in selection.disable_sources
                or key in selection.locations
            ):
                complete = False
                break
            manifest_source = next(
                (
                    candidate
                    for layout in plugin.layouts
                    if layout.id == "default"
                    for candidate in layout.sources
                    if candidate.id == source_id
                ),
                None,
            )
            if (
                manifest_source is None
                or source.paths != tuple(manifest_source.paths)
                or source.exact_files != tuple(manifest_source.exact_files)
                or source.required != manifest_source.required
            ):
                complete = False
                break
        if complete:
            eligible.add(profile)
    return frozenset(eligible)


def _paused_archive_warning(profiles: Sequence[str]) -> str:
    return (
        "archive work paused: recognized legacy profile(s) "
        + ", ".join(sorted(profiles))
        + " do not have their default complete log sources; "
        "checkpoint and pending batches are preserved"
    )


def _blocked(
    reason: str,
    effective: Optional[DesiredAlloyConfig],
    warnings: Sequence[str],
    *,
    changed: bool = False,
) -> HousekeepingResult:
    return HousekeepingResult(
        summary=HousekeepingSummary(status="Blocked"),
        changed=changed,
        warnings=_dedup(list(warnings) + [reason]),
        failed=True,
        effective_alloy=effective,
    )


def _apply_env_dropin(
    executor: "Executor",
    settings: "GlobalSettings",
    lxc_id: str,
    probe: "HousekeepingProbe",
    *,
    supported: bool,
    desired_env_hash: str,
    logging_request: Optional[Dict[str, Any]],
) -> Tuple[Optional[str], bool, "HousekeepingProbe"]:
    """Apply the owned journal-retention environment drop-in on observed drift."""
    if not supported or probe.policy_sha256.get("alloy_env", "") == desired_env_hash:
        return None, False, probe
    changed = False
    for command in alloy_env_commands(settings.housekeeping_local_retention_hours):
        applied = executor.housekeeping_apply(lxc_id, command=command)
        if applied.failed or applied.rc != 0:
            return (
                "failed to apply the Alloy journal-retention environment "
                f"drop-in: {_result_detail(applied)}",
                changed,
                probe,
            )
        changed = True
    try:
        verified = probe_housekeeping(executor, lxc_id, logging_request=logging_request)
    except Exception as exc:  # noqa: BLE001 - probe boundary
        return (
            f"could not verify the Alloy retention environment: {exc}",
            changed,
            probe,
        )
    if verified.policy_sha256.get("alloy_env", "") != desired_env_hash:
        return (
            "Alloy retention environment was not observed after configuration",
            changed,
            verified,
        )
    return None, changed, verified


def prepare_lxc_logging(
    executor: "Executor",
    settings: "GlobalSettings",
    *,
    node: str,
    cluster: str,
    lxc_id: str,
    name: str,
    base: DesiredAlloyConfig,
    catalogue: LogCatalogue,
    script_id: Optional[str],
    log_override: Optional[GuestLogOverride],
    dry_run: bool = False,
    allow_install: bool = False,
) -> HousekeepingResult:
    """Prepare live Alloy file collection for one guest (logging only).

    Selection is resolved from the declarative catalogue and the guest's
    already-extracted community-script identity; probe facts choose exactly one
    verified layout per selected owner/dependency.  The candidate HCL is
    validated by real Alloy before any ACL/environment/live-config mutation,
    scoped ``alloy`` ACLs are repaired for the resolved sources, the
    journal-retention environment drop-in is reconciled, and the exact desired
    config is deployed and re-verified for hash, access and service state.

    ``allow_install`` mirrors the ``--alloy-only`` installer behaviour: when
    ``False`` a missing Alloy binary or missing ACL tools block (the recurring
    maintenance path must never run APT); when ``True`` one install-only call
    provisions the missing Alloy binary and/or the guest ``acl`` package needed
    to measure selected file sources, after which the guest is re-probed rather
    than assuming access.

    A ``dry_run`` performs no staging/validation-only/install/ACL/environment/
    live mutation and returns an ``Audit`` summary with findings.

    An unsupported or missing community-script identity cannot disable baseline
    logging: the source-free layout is exactly the fleet's desired base config
    (system journal only), so it is validated and enforced like an intentional
    empty selection.  No application file source is fabricated or guessed, no
    application-scoped ACL is repaired, and the unsupported coverage evidence
    is retained as a diagnostic.
    """
    try:
        selection = select_log_plugins(
            catalogue,
            script_id=script_id,
            override=log_override,
            auto=settings.alloy_log_plugins_auto,
        )
        logging_request = (
            build_logging_request(selection)
            if selection.status == "selected"
            else None
        )
    except LogPluginError as exc:
        return _blocked(
            f"logging catalogue selection failed: {_sanitize_finding(str(exc))}",
            None,
            [],
        )

    try:
        probe = probe_housekeeping(executor, lxc_id, logging_request=logging_request)
    except Exception as exc:  # noqa: BLE001 - probe boundary stays best-effort
        return _blocked(f"housekeeping probe failed: {_sanitize_finding(str(exc))}", None, [])
    warnings: List[str] = list(probe.warnings)

    if selection.status == "unsupported":
        coverage = _sanitize_finding(
            "; ".join(selection.evidence) or "no recognized service profile"
        )
        warnings.append(
            "logging selection unsupported; preparing the fleet base "
            "system-journal configuration only and no application log files are "
            "configured: " + coverage
        )

    if not probe.is_running or probe.is_template:
        return HousekeepingResult(
            changed=False,
            warnings=_dedup(
                warnings
                + [f"guest {lxc_id} is stopped or a template; logging preparation skipped"]
            ),
            failed=False,
            effective_alloy=base if selection.status == "empty" else None,
        )

    if (
        selection.status == "selected"
        and probe.os_type not in _SUPPORTED_OS
    ):
        return _blocked(
            f"guest OS {probe.os_type or 'unknown'!r} is not Debian/apt-based; "
            "logging preparation is not attempted",
            None,
            warnings,
        )

    try:
        resolved = validate_log_layout(selection, probe.logging)
    except LogPluginError as exc:
        return _blocked(
            f"logging selection could not be verified: {_sanitize_finding(str(exc))}",
            None,
            warnings,
        )
    if resolved.blocked:
        return _blocked(
            "selected log sources have blocked or partial probe evidence; "
            "candidate syntax was not validated and Alloy was not changed",
            None,
            warnings,
        )

    try:
        candidate = _render_effective(
            base,
            node=node,
            cluster=cluster,
            lxc_id=lxc_id,
            name=name,
            sources=resolved.validation_candidate_sources,
            retention_hours=settings.housekeeping_local_retention_hours,
        )
    except Exception as exc:  # noqa: BLE001 - renderer validation boundary
        return _blocked(
            f"cannot render per-guest Alloy logging config: {_sanitize_finding(str(exc))}",
            None,
            warnings,
        )

    endpoint_conflict = False
    if settings.housekeeping_loki_url:
        endpoint = guest_write_endpoint(base.content)
        if endpoint is None:
            endpoint_conflict = True
            warnings.append(
                "could not confirm the guest Alloy write endpoint matches the "
                "configured housekeeping Loki base"
            )
        elif _endpoint_key(endpoint) != _endpoint_key(
            settings.housekeeping_loki_url.rstrip("/") + "/loki/api/v1/push"
        ):
            endpoint_conflict = True

    env_supported = base_uses_journal_env(base)
    desired_env_hash = _sha256(
        journal_env_content(settings.housekeeping_local_retention_hours)
    )
    env_drift = env_supported and probe.policy_sha256.get(
        "alloy_env", ""
    ) != desired_env_hash
    acl_needed = resolved.acl_repair_needed
    missing_tools = [tool for tool in _ACL_TOOLS if not probe.binaries.get(tool)]
    alloy_present = bool(probe.binaries.get("alloy"))

    if dry_run:
        findings = list(warnings)
        if endpoint_conflict:
            findings.append(
                "guest Alloy write endpoint conflicts with the configured housekeeping Loki base"
            )
        if missing_tools and resolved.sources:
            if resolved.unmeasurable:
                findings.append(
                    f"required ACL tools missing ({', '.join(missing_tools)}); "
                    "log source access is unmeasurable (collector prerequisite "
                    "missing); --alloy-only would install the guest 'acl' package "
                    "(dry run installs nothing)"
                )
            else:
                findings.append(
                    f"required ACL tools missing ({', '.join(missing_tools)}); "
                    "--alloy-only would install the guest 'acl' package "
                    "(dry run installs nothing)"
                )
        elif resolved.unmeasurable:
            findings.append(
                "log source access is unmeasurable (collector prerequisite missing)"
            )
        if acl_needed:
            if not probe.log_access_detail.get("alloy_user", False):
                findings.append(
                    "the 'alloy' user is absent from the guest; scoped read access "
                    "cannot be repaired"
                )
            else:
                findings.append(
                    "Alloy lacks scoped read/traversal/default access to the "
                    "selected log sources"
                )
        if not env_supported:
            findings.append(
                "custom desired journal source does not use "
                f"{FLEET_JOURNAL_MAX_AGE_VAR}; explicit migration required before "
                "tightening Alloy journal retention"
            )
        elif env_drift:
            findings.append(
                "the Alloy journal-retention environment drop-in would be written"
            )
        if not alloy_present:
            findings.append("Alloy is not installed")
        alloy_state = None
        try:
            alloy_state = _alloy_state(executor, lxc_id)
        except Exception:  # noqa: BLE001 - audit reports the finding only
            findings.append("Alloy state could not be read")
        if alloy_state is not None and (
            not alloy_state.binary_present
            or alloy_state.config_sha256 != candidate.sha256
        ):
            findings.append(
                "candidate Alloy syntax validation is pending (dry run)"
            )
        audit = reconcile_alloy(executor, candidate, dry_run=True, lxc_id=lxc_id)
        if audit.warning:
            findings.append(audit.warning)
        if selection.status == "selected":
            findings.extend(resolved.evidence)
            findings.extend(resolved.findings)
        if resolved.blocked or resolved.unmeasurable:
            effective: Optional[DesiredAlloyConfig] = None
        else:
            effective = candidate
        if findings:
            return HousekeepingResult(
                summary=HousekeepingSummary(status="Audit"),
                changed=False,
                warnings=_dedup(findings),
                failed=False,
                effective_alloy=effective,
            )
        return HousekeepingResult(
            changed=False,
            warnings=[],
            failed=False,
            effective_alloy=effective,
        )

    if endpoint_conflict:
        return _blocked(
            "guest Alloy write endpoint conflicts with the configured housekeeping Loki base",
            None,
            warnings,
        )

    changed = False
    try:
        alloy_state = _alloy_state(executor, lxc_id)
    except Exception as exc:  # noqa: BLE001 - probe boundary
        return _blocked(
            f"Alloy state could not be verified: {_sanitize_finding(str(exc))}",
            None,
            warnings,
        )

    # The explicit installer path (--alloy-only) is the single bootstrap action
    # for a missing Alloy *and* for the guest ACL prerequisite: the same
    # install-only step provisions both.  A helper without getfacl reports
    # selected file sources as unmeasurable, so this must run before that hard
    # block instead of keying off the resolved ACL repair state.  Recurring
    # maintenance (allow_install=False) never installs either prerequisite.
    needs_acl_tools = bool(allow_install and resolved.sources and missing_tools)
    if not alloy_state.binary_present or needs_acl_tools:
        if not allow_install:
            return _blocked(
                "Alloy is not installed; install it (or run --alloy-only) before "
                "preparing log collection",
                None,
                warnings,
            )
        install = executor.alloy_reconcile(
            lxc_id=lxc_id,
            desired_content=candidate.content,
            install=True,
            configure=False,
            add_journal_group=False,
            repair_service=False,
        )
        changed = True
        if install.failed or install.rc != 0:
            return _blocked(
                f"Alloy/ACL prerequisite installation failed: "
                f"{_result_detail(install)}",
                None,
                warnings,
                changed=changed,
            )
        try:
            alloy_state = _alloy_state(executor, lxc_id)
            probe = probe_housekeeping(
                executor, lxc_id, logging_request=logging_request
            )
            resolved = validate_log_layout(selection, probe.logging)
        except Exception as exc:  # noqa: BLE001 - probe boundary
            return _blocked(
                "Alloy/ACL prerequisites installed but the guest could not be "
                f"re-probed: {_sanitize_finding(str(exc))}",
                None,
                warnings,
                changed=changed,
            )
        warnings.extend(probe.warnings)
        if not alloy_state.binary_present:
            return _blocked(
                "Alloy was not observed after the install-only step",
                None,
                warnings,
                changed=changed,
            )
        if resolved.blocked:
            return _blocked(
                "selected log sources have blocked or partial probe evidence",
                None,
                warnings,
                changed=changed,
            )
        try:
            candidate = _render_effective(
                base,
                node=node,
                cluster=cluster,
                lxc_id=lxc_id,
                name=name,
                sources=resolved.validation_candidate_sources,
                retention_hours=settings.housekeeping_local_retention_hours,
            )
        except Exception as exc:  # noqa: BLE001 - renderer validation boundary
            return _blocked(
                f"cannot render per-guest Alloy logging config: "
                f"{_sanitize_finding(str(exc))}",
                None,
                warnings,
                changed=changed,
            )
        acl_needed = resolved.acl_repair_needed
        missing_tools = [tool for tool in _ACL_TOOLS if not probe.binaries.get(tool)]

    if resolved.unmeasurable:
        if missing_tools:
            return _blocked(
                f"required ACL tools missing ({', '.join(missing_tools)}); log "
                "source access could not be measured for the Alloy user; run "
                "--alloy-only to install the guest 'acl' package (recurring "
                "maintenance never installs packages)",
                None,
                warnings,
                changed=changed,
            )
        return _blocked(
            "log source access could not be measured for the Alloy user; "
            "recurring maintenance does not install collector prerequisites",
            None,
            warnings,
            changed=changed,
        )

    # Validate the exact candidate with real Alloy before touching ACLs, the
    # environment drop-in or the live config.  A config that is already the
    # observed active file has already passed that validation.
    if alloy_state.config_sha256 != candidate.sha256:
        validation = executor.alloy_reconcile(
            lxc_id=lxc_id,
            desired_content=candidate.content,
            install=False,
            configure=False,
            add_journal_group=False,
            repair_service=False,
            validate_only=True,
        )
        if validation.failed or validation.rc != 0:
            return _blocked(
                "candidate Alloy configuration failed validation: "
                f"{_result_detail(validation)}",
                None,
                warnings,
                changed=changed,
            )

    # Scoped permission repair, then re-probe to confirm real Alloy access
    # before reconciling the config that adds the file sources.
    if acl_needed:
        if missing_tools:
            return _blocked(
                f"required ACL tools missing ({', '.join(missing_tools)}); run "
                "--alloy-only to install the guest 'acl' package (recurring "
                "maintenance never installs packages)",
                None,
                warnings,
                changed=changed,
            )
        if not probe.log_access_detail.get("alloy_user", False):
            return _blocked(
                "the 'alloy' user is absent from the guest; install Alloy before "
                "preparing log permissions",
                None,
                warnings,
                changed=changed,
            )
        roots, exact_files = _log_acl_targets(
            resolved.validation_candidate_sources
        )
        if not roots and not exact_files:
            return _blocked(
                "selected log sources report denied Alloy access but declare no "
                "ACL targets",
                None,
                warnings,
                changed=changed,
            )
        try:
            applied = executor.housekeeping_log_acl(
                lxc_id, roots=roots, exact_files=exact_files
            )
        except Exception as exc:  # noqa: BLE001 - transport boundary, fail closed
            return _blocked(
                "failed to grant Alloy scoped log access: "
                f"{type(exc).__name__}",
                None,
                warnings,
                changed=changed,
            )
        acl_error, acl_changed = _log_acl_outcome(applied)
        if acl_error is not None:
            return _blocked(acl_error, None, warnings, changed=changed)
        changed = changed or acl_changed
        try:
            probe = probe_housekeeping(
                executor, lxc_id, logging_request=logging_request
            )
            resolved = validate_log_layout(selection, probe.logging)
        except Exception as exc:  # noqa: BLE001 - probe boundary
            return _blocked(
                f"could not verify Alloy log access after permission repair: "
                f"{_sanitize_finding(str(exc))}",
                None,
                warnings,
                changed=changed,
            )
        warnings.extend(probe.warnings)
        if resolved.blocked or resolved.unmeasurable:
            return _blocked(
                "log source evidence is blocked or unmeasurable after permission repair",
                None,
                warnings,
                changed=changed,
            )
        if resolved.acl_repair_needed:
            return _blocked(
                "Alloy still cannot read the selected log sources after permission repair",
                None,
                warnings,
                changed=changed,
            )

    # Journal-retention environment drop-in: only when the base journal source
    # reads the fleet variable, and only on observed content drift.
    if not env_supported:
        warnings.append(
            "custom desired journal source does not use "
            f"{FLEET_JOURNAL_MAX_AGE_VAR}; explicit migration is required before "
            "tightening Alloy journal retention"
        )
    else:
        env_error, env_changed, probe = _apply_env_dropin(
            executor,
            settings,
            lxc_id,
            probe,
            supported=env_supported,
            desired_env_hash=desired_env_hash,
            logging_request=logging_request,
        )
        changed |= env_changed
        if env_error is not None:
            return _blocked(env_error, None, warnings, changed=changed)

    final_sources = resolved.renderable_sources
    try:
        effective = _render_effective(
            base,
            node=node,
            cluster=cluster,
            lxc_id=lxc_id,
            name=name,
            sources=final_sources,
            retention_hours=settings.housekeeping_local_retention_hours,
        )
    except Exception as exc:  # noqa: BLE001 - renderer validation boundary
        return _blocked(
            f"cannot render per-guest Alloy logging config: {_sanitize_finding(str(exc))}",
            None,
            warnings,
            changed=changed,
        )

    # Deploy the exact desired config through the existing validation/restart
    # path, then re-probe for hash/access/service and source/layout drift.
    alloy_result = reconcile_alloy(executor, effective, dry_run=False, lxc_id=lxc_id)
    if alloy_result.warning:
        return _blocked(
            f"Alloy reconciliation failed: {alloy_result.warning}",
            None,
            warnings,
            changed=changed,
        )
    if alloy_result.changed or alloy_result.status:
        changed = True
    verified_alloy = alloy_result.probe
    if (
        verified_alloy is None
        or not verified_alloy.binary_present
        or verified_alloy.config_sha256 != effective.sha256
        or not verified_alloy.journal_member
        or not verified_alloy.service_enabled
        or not verified_alloy.service_active
    ):
        return _blocked(
            "Alloy configuration, service or hash was not observed after reconciliation",
            None,
            warnings,
            changed=changed,
        )
    try:
        probe = probe_housekeeping(
            executor, lxc_id, logging_request=logging_request
        )
        verified_resolved = validate_log_layout(selection, probe.logging)
    except Exception as exc:  # noqa: BLE001 - probe boundary
        return _blocked(
            f"could not verify Alloy log access after reconciliation: "
            f"{_sanitize_finding(str(exc))}",
            None,
            warnings,
            changed=changed,
        )
    warnings.extend(probe.warnings)
    drift = _resolution_drift(resolved, verified_resolved, final_sources)
    if drift is not None:
        return _blocked(
            f"log source evidence changed during preparation ({drift}); "
            "the verified config was not enforced",
            None,
            warnings,
            changed=changed,
        )
    if env_supported and probe.policy_sha256.get(
        "alloy_env", ""
    ) != desired_env_hash:
        return _blocked(
            "Alloy retention environment was not observed after reconciliation",
            None,
            warnings,
            changed=changed,
        )
    warnings.extend(_live_source_findings(verified_resolved))

    if changed:
        return HousekeepingResult(
            summary=HousekeepingSummary(status="Configured"),
            changed=True,
            warnings=_dedup(warnings),
            failed=False,
            effective_alloy=effective,
        )
    return HousekeepingResult(
        changed=False,
        warnings=_dedup(warnings),
        failed=False,
        effective_alloy=effective,
    )



def run_housekeeping(
    executor: Executor,
    settings: GlobalSettings,
    *,
    node: str,
    cluster: str,
    lxc_id: str,
    name: str,
    desired_alloy: Optional[DesiredAlloyConfig],
    catalogue: LogCatalogue,
    script_id: Optional[str],
    log_override: Optional[GuestLogOverride],
    dry_run: bool = False,
) -> HousekeepingResult:
    """Maintain one already-running guest without entering the update flow."""
    from proxmox_fleet.housekeeping_cache import clean_guest_caches
    from proxmox_fleet.housekeeping_retention import apply_guest_retention

    result = HousekeepingResult(effective_alloy=None)
    if matches_any(settings.exclude_list, cluster, lxc_id) or matches_any(
        settings.lxc_housekeeping_exclude_list, cluster, lxc_id
    ):
        return result
    if matches_any(settings.lxc_alloy_exclude_list, cluster, lxc_id):
        result.warnings.append("Housekeeping skipped: guest is excluded from Alloy management")
        return result

    # Selection is pure.  Validate it before cache/archive mutation so an
    # unknown override or ambiguous request fails without touching a guest.
    try:
        selection = select_log_plugins(
            catalogue,
            script_id=script_id,
            override=log_override,
            auto=settings.alloy_log_plugins_auto,
        )
        logging_request = (
            build_logging_request(selection)
            if selection.status == "selected"
            else None
        )
    except LogPluginError as exc:
        return _blocked(
            f"logging catalogue selection failed: {_sanitize_finding(str(exc))}",
            None,
            [],
        )

    try:
        probe = probe_housekeeping(executor, lxc_id, logging_request=logging_request)
    except Exception as exc:  # transport boundary; do not expose source/error bodies
        return _blocked(
            f"housekeeping probe unavailable ({type(exc).__name__})", None, []
        )
    if probe.is_template or not probe.is_running:
        return result
    result.warnings.extend(probe.warnings)
    if probe.os_type not in _SUPPORTED_OS:
        return _blocked(
            "housekeeping requires a supported Debian/apt guest",
            None,
            result.warnings,
        )

    reclaimed = 0
    archived = 0
    pruned = 0
    pending = False
    findings = False
    configured = False
    key = GuestKey(cluster, node, lxc_id)
    try:
        with CheckpointStore.for_history_dir(
            settings.fleet_history_dir, read_only=dry_run
        ) as store:
            # Cache safety/cadence does not depend on live delivery or backfill.
            caches = clean_guest_caches(executor, settings, store, key, probe, dry_run=dry_run)
            reclaimed += caches.bytes_reclaimed
            result.changed |= caches.changed
            result.failed |= caches.failed
            result.warnings.extend(caches.warnings)
            findings |= caches.findings
            loki_configured = True
            try:
                settings.require_loki_url()
            except ValueError:
                loki_configured = False
                result.failed = True
                result.warnings.append("Housekeeping requires a valid configured Loki base URL")
            if desired_alloy is None:
                result.failed = True
                result.warnings.append("Housekeeping requires the desired Alloy configuration")
            elif loki_configured:
                prepared = prepare_lxc_logging(
                    executor,
                    settings,
                    node=node,
                    cluster=cluster,
                    lxc_id=lxc_id,
                    name=name,
                    base=desired_alloy,
                    catalogue=catalogue,
                    script_id=script_id,
                    log_override=log_override,
                    dry_run=dry_run,
                    allow_install=False,
                )
                # Only preparation's verified result may be enforced — the
                # exact base when the selection has no sources (intentional
                # empty or unresolved identity). A rejected probe/selection
                # never falls back to base.
                result.effective_alloy = prepared.effective_alloy
                result.changed |= prepared.changed
                result.failed |= prepared.failed
                result.warnings.extend(prepared.warnings)
                findings |= prepared.summary is not None
                configured |= prepared.changed
                try:
                    resolved = validate_log_layout(selection, probe.logging)
                except LogPluginError:
                    resolved = None
                if resolved is None:
                    # Re-observe once; never infer archive eligibility from
                    # stale or incomplete layout facts.
                    try:
                        probe = probe_housekeeping(
                            executor, lxc_id, logging_request=logging_request
                        )
                        resolved = validate_log_layout(selection, probe.logging)
                    except Exception:  # noqa: BLE001 - fail closed below
                        resolved = None
                eligible = (
                    legacy_archive_profiles(selection, resolved)
                    if resolved is not None
                    else frozenset()
                )
                if (
                    not prepared.failed
                    and prepared.effective_alloy is None
                    and set(probe.recognized) - eligible
                ):
                    result.warnings.append(
                        _paused_archive_warning(
                            sorted(set(probe.recognized) - eligible)
                        )
                    )
                if not prepared.failed and prepared.effective_alloy is not None:
                    if prepared.changed:
                        probe = probe_housekeeping(
                            executor, lxc_id, logging_request=logging_request
                        )
                    imported = None
                    recognized = set(probe.recognized)
                    paused_profiles = sorted(recognized - eligible)
                    if paused_profiles:
                        result.warnings.append(
                            _paused_archive_warning(paused_profiles)
                        )
                        findings = True
                    else:
                        files = [
                            wire
                            for wire in probe.files
                            if wire["profile"] in probe.recognized
                        ]
                        # Ordinary updates may retain their existing concurrency,
                        # but bounded archive transports never overlap.
                        with _ARCHIVE_LOCK:
                            imported = import_guest_logs(
                                executor, settings, store, key, name=name,
                                files=files, dry_run=dry_run,
                            )
                        archived += imported.bytes_archived
                        result.failed |= imported.failed
                        result.warnings.extend(imported.warnings)
                        pending = imported.pending
                        findings |= bool(imported.warnings or imported.pending)
                    initial = store.initial_manifest_state(key)
                    if imported is not None:
                        pending |= initial.remaining > 0 or initial.pending_batch
                    can_retain = imported is None or dry_run or (
                        not imported.failed and not pending
                    )
                    if can_retain:
                        retained = apply_guest_retention(
                            executor, settings, store, key, probe,
                            prepared.effective_alloy,
                            covered_files=(
                                imported.covered_files if imported is not None else ()
                            ),
                            archive_profiles=eligible,
                            dry_run=dry_run,
                        )
                        reclaimed += retained.bytes_reclaimed
                        pruned += retained.files_pruned
                        result.changed |= retained.changed
                        result.failed |= retained.failed
                        result.warnings.extend(retained.warnings)
                        findings |= retained.findings
                        configured |= retained.changed
                    if imported is not None and not dry_run:
                        files = [
                            wire
                            for wire in probe.files
                            if wire["profile"] in probe.recognized
                        ]
                        present = {
                            (
                                wire["profile"], wire["log_kind"], wire["device"], wire["inode"],
                                logical_identity(wire["profile"], wire["log_kind"], wire["path"]),
                            )
                            for wire in files
                        }
                        for source in store.sources(key):
                            canonical = SourceProvenance.parse(source.provenance).canonical_path or source.path
                            identity = (
                                source.profile, source.log_kind, source.device, source.inode,
                                logical_identity(source.profile, source.log_kind, canonical),
                            )
                            if identity in present:
                                if source.absent:
                                    store.clear_source_absent(key, source.source_id)
                            elif not source.absent:
                                store.mark_source_absent(key, source.source_id)
                        store.purge_tombstones()
    except Exception as exc:  # fail closed without turning maintenance into update rollback
        result.failed = True
        result.warnings.append(f"Housekeeping stopped safely ({type(exc).__name__}); file logs retained")

    result.warnings = _dedup(result.warnings)
    result.changed |= archived > 0
    status: Optional[Literal["Configured", "Cleaned", "Backfill pending", "Blocked", "Audit"]] = None
    if result.failed:
        status = "Blocked"
    elif dry_run and (findings or pending or result.warnings):
        status = "Audit"
    elif pending:
        status = "Backfill pending"
    elif reclaimed > 0 or pruned > 0:
        status = "Cleaned"
    elif configured or archived > 0:
        status = "Configured"
    if status is not None:
        result.summary = HousekeepingSummary(
            status=status, bytes_reclaimed=reclaimed, bytes_archived=archived,
            files_pruned=pruned,
        )
    return result

