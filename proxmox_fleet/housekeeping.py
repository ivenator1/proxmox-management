"""Manager-owned LXC logging, acknowledged archival and maintenance policy.

Python owns live logging preparation, acknowledged archival, gated retention
and fixed native cache profiles. Ansible transports only exact operations.
Recurring maintenance never installs packages or starts stopped guests.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Sequence, Set, Tuple
from urllib.parse import urlsplit

from proxmox_fleet.alloy import (
    DesiredAlloyConfig,
    reconcile_alloy,
    render_lxc_log_config,
)
from proxmox_fleet import housekeeping_native as native
from proxmox_fleet.cluster import matches_any
from proxmox_fleet.housekeeping_checkpoint import CheckpointStore, GuestKey
from proxmox_fleet.housekeeping_import import import_guest_logs
from proxmox_fleet.housekeeping_io import (
    NPM_LOG_ROOT,
    PBS_API_ROOT,
    PBS_TASK_ROOT,
    POLICY_FILES,
    QUARANTINE_DIRNAME,
    REQUIRED_BINARIES,
)
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

_LOG_ROOTS: Dict[str, Tuple[str, ...]] = {
    "npm": (NPM_LOG_ROOT,),
    "pbs": (PBS_TASK_ROOT, PBS_API_ROOT),
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


def _require_complete_probe(facts: Dict[str, Any]) -> None:
    """Fail closed on a truncated, missing or malformed probe fact stream.

    The runner has silently dropped oversized results before; an incomplete
    probe must never be read as emptiness and reported as success.
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
    running = bool(guest.get("is_running"))
    if running and not bool(guest.get("is_template")):
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


def probe_housekeeping(executor: "Executor", lxc_id: str) -> HousekeepingProbe:
    """Run the read-only probe and return its typed facts.

    Raises :class:`HousekeepingError` when the probe primitive itself fails or
    returns an incomplete/malformed fact stream, so callers never mistake a
    failed or truncated enumeration for emptiness.
    """
    result = executor.housekeeping_probe(lxc_id)
    if result.failed or result.rc != 0:
        raise HousekeepingError(f"housekeeping probe failed: {_result_detail(result)}")
    facts = _as_dict(result.facts)
    _require_complete_probe(facts)
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


def acl_repair_commands(profiles: Set[str]) -> List[str]:
    """Exact fixed-root commands that grant ``alloy`` scoped log access.

    Ancestors (``/data``, ``/var/log/proxmox-backup``) receive traverse-only
    access; each log root is walked without following symlinks (``find -P``),
    bounded to one filesystem (``-xdev``), with the quarantine tree pruned.
    Directories get ``rX`` plus an inheritable default ``r-x``; files get ``rX``.
    No broad ``backup`` group or world readability is ever granted.
    """
    roots: List[str] = []
    for profile in ("npm", "pbs"):
        if profile in profiles:
            for root in _LOG_ROOTS[profile]:
                if root not in roots:
                    roots.append(root)
    ancestors: List[str] = []
    for root in roots:
        ancestor = posixpath.dirname(root)
        if ancestor and ancestor not in ancestors:
            ancestors.append(ancestor)
    commands = [f"setfacl -m u:alloy:x -- {shlex.quote(ancestor)}" for ancestor in ancestors]
    for root in roots:
        path = shlex.quote(root)
        prune = f"-name {QUARANTINE_DIRNAME} -prune -o"
        commands.append(
            f"find -P {path} -xdev {prune} -type d "
            f"-exec setfacl -m u:alloy:rX,d:u:alloy:r-x -- {{}} +"
        )
        commands.append(
            f"find -P {path} -xdev {prune} -type f "
            f"-exec setfacl -m u:alloy:rX -- {{}} +"
        )
    return commands


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
    profiles: Set[str],
    retention_hours: int,
) -> DesiredAlloyConfig:
    """Append file-source stanzas only for recognized profiles.

    Profile-free guests keep the base config byte-for-byte, so a guest with no
    NPM/PBS surface never gains a generated component.
    """
    if not profiles:
        return base
    return render_lxc_log_config(
        base,
        node=node,
        cluster=cluster,
        lxc_id=lxc_id,
        name=name,
        profiles=profiles,
        retention_hours=retention_hours,
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


def prepare_lxc_logging(
    executor: "Executor",
    settings: "GlobalSettings",
    *,
    node: str,
    cluster: str,
    lxc_id: str,
    name: str,
    base: DesiredAlloyConfig,
    dry_run: bool = False,
    allow_install: bool = False,
) -> HousekeepingResult:
    """Prepare live Alloy file collection for one guest (logging only).

    Probes the guest, renders the per-guest config for discovered NPM/PBS
    profiles, repairs scoped ``alloy`` ACLs, reconciles the journal-retention
    environment drop-in, and deploys the effective config through the existing
    Alloy validation path.  It never archives/imports, prunes files, cleans
    caches, tightens journald retention or installs recurring packages.

    ``allow_install`` mirrors the ``--alloy-only`` installer behaviour: when
    ``False`` a missing Alloy binary blocks (the recurring maintenance path must
    never run APT); when ``True`` the existing installer-capable reconciler may
    install it first.

    A ``dry_run`` performs no guest or checkpoint writes and returns an ``Audit``
    summary with findings, or no summary when the guest is already compliant.
    Any failure or unsafe precondition returns ``Blocked`` with the rendered
    effective config attached (once rendering succeeded) so later Alloy
    reconciliation can never restore a journal-only base over managed file
    sources.
    """
    warnings: List[str] = []
    try:
        probe = probe_housekeeping(executor, lxc_id)
    except Exception as exc:  # noqa: BLE001 - probe boundary stays best-effort
        return HousekeepingResult(
            summary=HousekeepingSummary(status="Blocked"),
            changed=False,
            warnings=[f"housekeeping probe failed: {exc}"],
            failed=True,
            effective_alloy=None,
        )
    warnings.extend(probe.warnings)

    if not probe.is_running or probe.is_template:
        return HousekeepingResult(
            changed=False,
            warnings=_dedup(
                warnings
                + [f"guest {lxc_id} is stopped or a template; logging preparation skipped"]
            ),
            failed=False,
            effective_alloy=base,
        )

    profiles = set(probe.recognized)
    try:
        effective = _render_effective(
            base,
            node=node,
            cluster=cluster,
            lxc_id=lxc_id,
            name=name,
            profiles=profiles,
            retention_hours=settings.housekeeping_local_retention_hours,
        )
    except Exception as exc:  # noqa: BLE001 - renderer validation boundary
        return HousekeepingResult(
            summary=HousekeepingSummary(status="Blocked"),
            changed=False,
            warnings=_dedup(
                warnings + [f"cannot render per-guest Alloy logging config: {exc}"]
            ),
            failed=True,
            effective_alloy=None,
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
    env_drift = env_supported and probe.policy_sha256.get("alloy_env", "") != desired_env_hash
    acl_needed = bool(profiles) and not probe.log_access_ready
    missing_tools = [tool for tool in _ACL_TOOLS if not probe.binaries.get(tool)]
    alloy_present = bool(probe.binaries.get("alloy"))
    unsupported_os = bool(profiles) and probe.os_type not in _SUPPORTED_OS

    if dry_run:
        findings = list(warnings)
        if endpoint_conflict:
            findings.append(
                "guest Alloy write endpoint conflicts with the configured housekeeping Loki base"
            )
        if unsupported_os:
            findings.append(
                f"guest OS {probe.os_type or 'unknown'!r} is not Debian/apt-based"
            )
        if acl_needed:
            if missing_tools:
                findings.append(
                    f"required ACL tools missing ({', '.join(missing_tools)}); "
                    "the guest needs the 'acl' package"
                )
            else:
                findings.append(
                    "Alloy lacks scoped read access to the discovered log trees"
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
        audit = reconcile_alloy(executor, effective, dry_run=True, lxc_id=lxc_id)
        if audit.warning:
            findings.append(audit.warning)
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
            effective,
            warnings,
        )
    if unsupported_os:
        return _blocked(
            f"guest OS {probe.os_type or 'unknown'!r} is not Debian/apt-based; "
            "logging preparation is not attempted",
            effective,
            warnings,
        )

    changed = False
    blocked_reason: Optional[str] = None

    # Alloy must exist before its config can be deployed.  Recurring maintenance
    # (allow_install=False) never installs it implicitly; --alloy-only may.
    if not alloy_present:
        if not allow_install:
            return _blocked(
                "Alloy is not installed; install it (or run --alloy-only) before "
                "preparing log collection",
                effective,
                warnings,
            )
        install = reconcile_alloy(executor, effective, dry_run=False, lxc_id=lxc_id)
        if install.warning or not (install.probe and install.probe.binary_present):
            return _blocked(
                f"Alloy installation failed: {install.warning or 'unknown error'}",
                effective,
                warnings,
                changed=True,
            )
        changed = True
        try:
            probe = probe_housekeeping(executor, lxc_id)
        except Exception as exc:  # noqa: BLE001 - probe boundary
            return _blocked(
                f"Alloy installed but the guest could not be re-probed: {exc}",
                effective,
                warnings,
                changed=True,
            )
        warnings.extend(probe.warnings)
        profiles = set(probe.recognized)
        acl_needed = bool(profiles) and not probe.log_access_ready
        missing_tools = [tool for tool in _ACL_TOOLS if not probe.binaries.get(tool)]

    # Scoped permission repair, then re-probe to confirm real Alloy access
    # before reconciling the config that adds the file sources.
    if acl_needed and blocked_reason is None:
        if missing_tools:
            blocked_reason = (
                f"required ACL tools missing ({', '.join(missing_tools)}); install "
                "the 'acl' package in the guest (recurring maintenance never installs packages)"
            )
        elif not probe.log_access_detail.get("alloy_user", False):
            blocked_reason = (
                "the 'alloy' user is absent from the guest; install Alloy before "
                "preparing log permissions"
            )
        else:
            for command in acl_repair_commands(profiles):
                applied = executor.housekeeping_apply(lxc_id, command=command)
                if applied.failed or applied.rc != 0:
                    blocked_reason = (
                        f"failed to grant Alloy scoped log access: {_result_detail(applied)}"
                    )
                    break
                changed = True
            if blocked_reason is None:
                try:
                    verified = probe_housekeeping(executor, lxc_id)
                except Exception as exc:  # noqa: BLE001 - probe boundary
                    blocked_reason = (
                        f"could not verify Alloy log access after permission repair: {exc}"
                    )
                else:
                    if not verified.log_access_ready:
                        blocked_reason = (
                            "Alloy still cannot read the discovered log trees after "
                            "permission repair"
                        )
                    probe = verified

    # Journal-retention environment drop-in: only when the base journal source
    # reads the fleet variable, and only on content drift.
    if blocked_reason is None:
        if not env_supported:
            warnings.append(
                "custom desired journal source does not use "
                f"{FLEET_JOURNAL_MAX_AGE_VAR}; explicit migration is required before "
                "tightening Alloy journal retention"
            )
        elif env_drift:
            for command in alloy_env_commands(settings.housekeeping_local_retention_hours):
                applied = executor.housekeeping_apply(lxc_id, command=command)
                if applied.failed or applied.rc != 0:
                    blocked_reason = (
                        "failed to apply the Alloy journal-retention environment "
                        f"drop-in: {_result_detail(applied)}"
                    )
                    break
                changed = True
            if blocked_reason is None:
                try:
                    verified = probe_housekeeping(executor, lxc_id)
                except HousekeepingError as exc:
                    blocked_reason = f"could not verify the Alloy retention environment: {exc}"
                else:
                    if verified.policy_sha256.get("alloy_env", "") != desired_env_hash:
                        blocked_reason = "Alloy retention environment was not observed after configuration"
                    probe = verified

    # Deploy the effective config through the existing validation/restart path.
    if blocked_reason is None:
        alloy_result = reconcile_alloy(executor, effective, dry_run=False, lxc_id=lxc_id)
        if alloy_result.warning:
            blocked_reason = f"Alloy reconciliation failed: {alloy_result.warning}"
        elif alloy_result.changed or alloy_result.status:
            changed = True

    if blocked_reason is not None:
        return _blocked(blocked_reason, effective, warnings, changed=changed)
    if changed:
        return HousekeepingResult(
            summary=HousekeepingSummary(status="Configured"),
            changed=True,
            warnings=warnings,
            failed=False,
            effective_alloy=effective,
        )
    return HousekeepingResult(
        changed=False,
        warnings=warnings,
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
    dry_run: bool = False,
) -> HousekeepingResult:
    """Maintain one already-running guest without entering the update flow."""
    from proxmox_fleet.housekeeping_cache import clean_guest_caches
    from proxmox_fleet.housekeeping_retention import apply_guest_retention

    result = HousekeepingResult(effective_alloy=desired_alloy)
    if matches_any(settings.exclude_list, cluster, lxc_id) or matches_any(
        settings.lxc_housekeeping_exclude_list, cluster, lxc_id
    ):
        return result
    if matches_any(settings.lxc_alloy_exclude_list, cluster, lxc_id):
        result.warnings.append("Housekeeping skipped: guest is excluded from Alloy management")
        return result
    try:
        probe = probe_housekeeping(executor, lxc_id)
    except Exception as exc:  # transport boundary; do not expose source/error bodies
        return _blocked(
            f"housekeeping probe unavailable ({type(exc).__name__})", desired_alloy, []
        )
    if probe.is_template or not probe.is_running:
        return result
    result.warnings.extend(probe.warnings)
    if probe.os_type not in _SUPPORTED_OS:
        return _blocked("housekeeping requires a supported Debian/apt guest", desired_alloy, result.warnings)

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
                # The first trusted probe is enough to retain the generated
                # desired pipeline if preparation's second probe is unavailable.
                result.effective_alloy = _render_effective(
                    desired_alloy, node=node, cluster=cluster, lxc_id=lxc_id,
                    name=name, profiles=set(probe.recognized),
                    retention_hours=settings.housekeeping_local_retention_hours,
                )
                prepared = prepare_lxc_logging(
                    executor, settings, node=node, cluster=cluster, lxc_id=lxc_id,
                    name=name, base=desired_alloy, dry_run=dry_run, allow_install=False,
                )
                # A failed second probe must not replace the generated desired
                # pipeline with an unverified journal-only base.
                if prepared.effective_alloy is not None:
                    result.effective_alloy = prepared.effective_alloy
                result.changed |= prepared.changed
                result.failed |= prepared.failed
                result.warnings.extend(prepared.warnings)
                findings |= prepared.summary is not None
                configured |= prepared.changed
                if not prepared.failed and prepared.effective_alloy is not None:
                    if prepared.changed:
                        probe = probe_housekeeping(executor, lxc_id)
                    files = [wire for wire in probe.files if wire["profile"] in probe.recognized]
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
                    pending |= initial.remaining > 0 or initial.pending_batch
                    if dry_run or (not imported.failed and not pending):
                        retained = apply_guest_retention(
                            executor, settings, store, key, probe, prepared.effective_alloy,
                            covered_files=imported.covered_files, dry_run=dry_run,
                        )
                        reclaimed += retained.bytes_reclaimed
                        pruned += retained.files_pruned
                        result.changed |= retained.changed
                        result.failed |= retained.failed
                        result.warnings.extend(retained.warnings)
                        findings |= retained.findings
                        configured |= retained.changed
                    if not dry_run:
                        present = {(wire["device"], wire["inode"]) for wire in files}
                        for source in store.sources(key):
                            if (source.device, source.inode) in present:
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
