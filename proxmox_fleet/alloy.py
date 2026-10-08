"""Desired-state compliance for Grafana Alloy on managed LXC and VM guests.

Python owns drift classification and reconciliation policy.  The executor only
runs the direct-guest or pct-mediated probe/deploy primitives selected here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Set

from proxmox_fleet.housekeeping_io import QUARANTINE_DIRNAME
from proxmox_fleet.runner import PrimitiveResult


PLACEHOLDER_MARKER = "REPLACE_WITH_LOKI_ENDPOINT"

# Reserved component names for the fleet-managed file-log pipeline rendered by
# render_lxc_log_config().  A desired base config must never declare any of
# these; the renderer refuses to append a second copy rather than silently
# producing an invalid or ambiguous Alloy configuration.
FLEET_PROCESS_COMPONENT = "fleet_file_pack"
FLEET_NPM_SOURCE_COMPONENT = "fleet_npm"
FLEET_PBS_TASKS_SOURCE_COMPONENT = "fleet_pbs_tasks"
FLEET_PBS_API_SOURCE_COMPONENT = "fleet_pbs_api"
RESERVED_COMPONENT_NAMES: frozenset[str] = frozenset(
    {
        FLEET_PROCESS_COMPONENT,
        FLEET_NPM_SOURCE_COMPONENT,
        FLEET_PBS_TASKS_SOURCE_COMPONENT,
        FLEET_PBS_API_SOURCE_COMPONENT,
    }
)

# Only these probe-reported profiles have a defined live file-source mapping.
SUPPORTED_LOG_PROFILES: frozenset[str] = frozenset({"npm", "pbs"})

# Exact live (non-archive) discovery globs.  Rotated/compressed archives are
# handled by the acknowledged importer, never by these readers.
NPM_LOG_ROOT = "/data/logs"
PBS_TASKS_ROOT = "/var/log/proxmox-backup/tasks"
NPM_LIVE_LOG_GLOB = f"{NPM_LOG_ROOT}/**/*.log"
PBS_LIVE_TASKS_GLOB = f"{PBS_TASKS_ROOT}/[0-9A-F][0-9A-F]/UPID:*"
PBS_LIVE_API_GLOBS = (
    "/var/log/proxmox-backup/api/access.log",
    "/var/log/proxmox-backup/api/auth.log",
)


class AlloyConfigError(ValueError):
    """The desired guest Alloy config is unsafe or unavailable."""


@dataclass(frozen=True)
class DesiredAlloyConfig:
    content: str
    sha256: str


@dataclass(frozen=True)
class AlloyProbe:
    binary_present: bool
    package_manager: str
    config_sha256: str
    journal_member: bool
    service_enabled: bool
    service_active: bool

    def drift(self, desired_sha256: str) -> list[str]:
        issues: list[str] = []
        if not self.binary_present:
            issues.append("Alloy is not installed")
        if self.config_sha256 != desired_sha256:
            issues.append("configuration differs")
        if not self.journal_member:
            issues.append("alloy is not in systemd-journal")
        if not self.service_enabled:
            issues.append("service is not enabled")
        if not self.service_active:
            issues.append("service is not active")
        return issues


@dataclass(frozen=True)
class AlloyResult:
    status: Optional[str] = None
    changed: bool = False
    warning: Optional[str] = None
    probe: Optional[AlloyProbe] = None


# Installer registry is deliberately transport-independent.  A future dnf/apk
# installer adds an entry and primitive implementation without changing flows.
INSTALLERS: Dict[str, str] = {"apt": "grafana-apt-stable"}


class AlloyExecutor(Protocol):
    """Minimal transport adapter needed by the compliance engine."""

    host: str

    def alloy_probe(self, *, lxc_id: Optional[str] = None) -> PrimitiveResult:
        ...

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
        ...


def load_desired_config(path: str | Path) -> DesiredAlloyConfig:
    """Read and hash the desired config, rejecting unsafe placeholder input."""
    config_path = Path(path)
    try:
        content = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AlloyConfigError(f"cannot read {config_path}: {exc}") from exc
    if not content.strip():
        raise AlloyConfigError(f"{config_path} is empty")
    if PLACEHOLDER_MARKER in content:
        raise AlloyConfigError(
            f"{config_path} still contains the {PLACEHOLDER_MARKER} placeholder"
        )
    return DesiredAlloyConfig(
        content=content,
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def parse_probe(facts: Dict[str, Any]) -> AlloyProbe:
    """Convert primitive facts into the typed compliance view."""
    return AlloyProbe(
        binary_present=_as_bool(facts.get("binary_present", False)),
        package_manager=str(facts.get("package_manager", "unknown")).strip().lower() or "unknown",
        config_sha256=str(facts.get("config_sha256", "")).strip().lower(),
        journal_member=_as_bool(facts.get("journal_member", False)),
        service_enabled=_as_bool(facts.get("service_enabled", False)),
        service_active=_as_bool(facts.get("service_active", False)),
    )


def _result_detail(result: Any) -> str:
    detail = str(result.stderr or result.stdout or "primitive failed").strip()
    return " ".join(detail.split())[-400:]


def _probe(executor: AlloyExecutor, *, lxc_id: Optional[str]) -> AlloyProbe:
    result: Any = executor.alloy_probe(lxc_id=lxc_id)
    if result.failed or result.rc != 0:
        raise RuntimeError(_result_detail(result))
    return parse_probe(result.facts)


def _changed_status(before: AlloyProbe, final: AlloyProbe, desired_sha256: str) -> Optional[str]:
    if not before.binary_present and final.binary_present:
        return "Installed"
    if before.config_sha256 != desired_sha256 and final.config_sha256 == desired_sha256:
        return "Configured"
    service_before = (
        not before.journal_member
        or not before.service_enabled
        or not before.service_active
    )
    service_final = final.journal_member and final.service_enabled and final.service_active
    if service_before and service_final:
        return "Service repaired"
    return None


def reconcile_alloy(
    executor: AlloyExecutor,
    desired: DesiredAlloyConfig,
    *,
    dry_run: bool = False,
    lxc_id: Optional[str] = None,
) -> AlloyResult:
    """Probe and best-effort reconcile one guest.

    Every failure is returned as a warning.  Callers deliberately do not fold it
    into fleet failure state, so ordinary package work can continue.
    """
    try:
        before = _probe(executor, lxc_id=lxc_id)
    except Exception as exc:  # noqa: BLE001 - best-effort compliance boundary
        return AlloyResult(warning=f"Alloy probe failed: {exc}")

    drift = before.drift(desired.sha256)
    if not drift:
        return AlloyResult(probe=before)

    if not before.binary_present and before.package_manager not in INSTALLERS:
        return AlloyResult(
            warning=(
                f"Alloy is missing and automatic installation is unavailable for "
                f"package manager {before.package_manager!r}"
            ),
            probe=before,
        )

    if dry_run:
        return AlloyResult(
            warning="Alloy drift detected (audit only): " + "; ".join(drift),
            probe=before,
        )

    install = not before.binary_present
    configure = install or before.config_sha256 != desired.sha256
    add_journal_group = install or not before.journal_member
    repair_service = (
        install
        or configure
        or add_journal_group
        or not before.service_enabled
        or not before.service_active
    )

    deploy_result: Any = None
    deploy_error: Optional[str] = None
    try:
        deploy_result = executor.alloy_reconcile(
            lxc_id=lxc_id,
            desired_content=desired.content,
            install=install,
            configure=configure,
            add_journal_group=add_journal_group,
            repair_service=repair_service,
        )
        if deploy_result.failed or deploy_result.rc != 0:
            deploy_error = _result_detail(deploy_result)
    except Exception as exc:  # noqa: BLE001 - best-effort compliance boundary
        deploy_error = str(exc)

    try:
        final = _probe(executor, lxc_id=lxc_id)
    except Exception as exc:  # noqa: BLE001
        changed = False
        if deploy_result is not None:
            changed = bool(deploy_result.changed)
        detail = str(exc)
        if deploy_error is not None:
            detail = deploy_error
        return AlloyResult(
            changed=changed,
            warning=f"Alloy reconciliation could not be verified: {detail}",
            probe=before,
        )

    status = _changed_status(before, final, desired.sha256)
    deploy_changed = False if deploy_result is None else bool(deploy_result.changed)
    changed = status is not None or deploy_changed
    remaining = final.drift(desired.sha256)
    if deploy_error or remaining:
        reasons = []
        if deploy_error:
            reasons.append(deploy_error)
        if remaining:
            reasons.append("unresolved: " + "; ".join(remaining))
        return AlloyResult(
            status=status,
            changed=changed,
            warning="Alloy reconciliation failed: " + " | ".join(reasons),
            probe=final,
        )
    return AlloyResult(status=status, changed=changed, probe=final)


def _hcl_string(value: str) -> str:
    """Emit an HCL string literal using JSON escaping (HCL-compatible)."""
    return json.dumps(value, ensure_ascii=False)


def _reserved_conflicts(content: str) -> list[str]:
    """Return reserved fleet component names already present in the base HCL."""
    return sorted(
        name
        for name in RESERVED_COMPONENT_NAMES
        if re.search(rf"\b{re.escape(name)}\b", content)
    )


def _quarantine_exclude(root: str) -> str:
    """Glob that excludes every quarantine tree below a managed log root."""
    return f"{root}/**/{QUARANTINE_DIRNAME}/**"


def _fleet_target(
    *,
    path: str,
    app: str,
    log_kind: str,
    cluster: str,
    node: str,
    lxc_id: str,
    name: str,
    exclude: Optional[str] = None,
) -> list[tuple[str, str]]:
    """Build one file-match target map with the shared live-delivery labels."""
    fields: list[tuple[str, str]] = [("__path__", path)]
    if exclude is not None:
        fields.append(("__path_exclude__", exclude))
    fields.extend(
        [
            ("job", "lxc-file"),
            ("delivery", "live"),
            ("cluster", cluster),
            ("node", node),
            ("guest_id", lxc_id),
            ("host", name),
            ("app", app),
            ("log_kind", log_kind),
        ]
    )
    return fields


def _source_stanza(
    component: str,
    targets: list[list[tuple[str, str]]],
    retention_hours: int,
) -> str:
    lines = [f'loki.source.file "{component}" {{', "  targets = ["]
    for target in targets:
        lines.append("    {")
        for key, value in target:
            lines.append(f"      {key} = {_hcl_string(value)},")
        lines.append("    },")
    lines.append("  ]")
    lines.append(f"  forward_to = [loki.process.{FLEET_PROCESS_COMPONENT}.receiver]")
    lines.append("  tail_from_end = false")
    lines.append('  on_positions_file_error = "restart_from_beginning"')
    lines.append("")
    lines.append("  file_match {")
    lines.append("    enabled = true")
    lines.append('    sync_period = "10s"')
    lines.append(f'    ignore_older_than = "{retention_hours}h"')
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines)


def _process_stanza() -> str:
    return (
        f'loki.process "{FLEET_PROCESS_COMPONENT}" {{\n'
        "  forward_to = [loki.write.default.receiver]\n"
        "\n"
        "  stage.pack {\n"
        '    labels = ["filename"]\n'
        "    ingest_timestamp = true\n"
        "  }\n"
        "}"
    )


def render_lxc_log_config(
    base: DesiredAlloyConfig,
    *,
    node: str,
    cluster: str,
    lxc_id: str,
    name: str,
    profiles: Set[str],
    retention_hours: int,
) -> DesiredAlloyConfig:
    """Append fleet-managed live file sources to a guest's desired Alloy HCL.

    The base content, journal component names and storage path are preserved
    verbatim; only the discovered-profile sources, the shared packing process
    and the exact resulting content hash are added.  Unknown profiles and base
    configs that already use a reserved component name are rejected before any
    deployment.
    """
    wanted = set(profiles)
    unknown = sorted(wanted - SUPPORTED_LOG_PROFILES)
    if unknown:
        raise AlloyConfigError("unsupported log profile(s): " + ", ".join(unknown))
    if not wanted:
        return base
    hours = int(retention_hours)
    if hours < 1:
        raise AlloyConfigError("retention_hours must be a positive integer")
    conflicts = _reserved_conflicts(base.content)
    if conflicts:
        raise AlloyConfigError(
            "desired config already uses reserved fleet component name(s): "
            + ", ".join(conflicts)
        )

    blocks = [_process_stanza()]
    if "npm" in wanted:
        blocks.append(
            _source_stanza(
                FLEET_NPM_SOURCE_COMPONENT,
                [
                    _fleet_target(
                        path=NPM_LIVE_LOG_GLOB,
                        exclude=_quarantine_exclude(NPM_LOG_ROOT),
                        app="nginxproxymanager",
                        log_kind="application",
                        cluster=cluster,
                        node=node,
                        lxc_id=lxc_id,
                        name=name,
                    )
                ],
                hours,
            )
        )
    if "pbs" in wanted:
        blocks.append(
            _source_stanza(
                FLEET_PBS_TASKS_SOURCE_COMPONENT,
                [
                    _fleet_target(
                        path=PBS_LIVE_TASKS_GLOB,
                        exclude=_quarantine_exclude(PBS_TASKS_ROOT),
                        app="proxmox-backup",
                        log_kind="task",
                        cluster=cluster,
                        node=node,
                        lxc_id=lxc_id,
                        name=name,
                    )
                ],
                hours,
            )
        )
        blocks.append(
            _source_stanza(
                FLEET_PBS_API_SOURCE_COMPONENT,
                [
                    _fleet_target(
                        path=api_path,
                        app="proxmox-backup",
                        log_kind="api",
                        cluster=cluster,
                        node=node,
                        lxc_id=lxc_id,
                        name=name,
                    )
                    for api_path in PBS_LIVE_API_GLOBS
                ],
                hours,
            )
        )

    content = base.content
    if not content.endswith("\n"):
        content += "\n"
    content += "\n" + "\n\n".join(blocks) + "\n"
    return DesiredAlloyConfig(
        content=content,
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
