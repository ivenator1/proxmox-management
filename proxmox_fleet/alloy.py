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
from typing import Any, Dict, Iterable, Optional, Protocol, Sequence

from proxmox_fleet.housekeeping_io import QUARANTINE_DIRNAME
from proxmox_fleet.log_plugins import ResolvedLogSource
from proxmox_fleet.models.log_plugins import MultilineOptions, patterns_overlap
from proxmox_fleet.runner import PrimitiveResult


PLACEHOLDER_MARKER = "REPLACE_WITH_LOKI_ENDPOINT"

# Reserved component name for the fleet-managed file-log pipeline rendered by
# render_lxc_log_config().  A desired base config must never declare this (or a
# selected source component); the renderer refuses to append a second copy
# rather than silently producing an invalid or ambiguous Alloy configuration.
FLEET_PROCESS_COMPONENT = "fleet_file_pack"
RESERVED_COMPONENT_NAMES: frozenset[str] = frozenset({FLEET_PROCESS_COMPONENT})

# Source statuses whose descriptor/layout evidence is safe to render.  Only
# ``active``/``quiet`` may reach final deployment; ``acl-needed`` is additionally
# accepted so a caller can validate candidate HCL before repairing scoped ACLs
# (the caller still re-probes before replacing the live configuration).
RENDERABLE_SOURCE_STATUSES: frozenset[str] = frozenset(
    {"active", "quiet", "acl-needed"}
)

_COMPONENT_NAME_RE = re.compile(r"^fleet_[a-z0-9][a-z0-9_]{0,63}$")

# Automatic quarantine exclusion emitted by the source resolver for every
# managed directory root: ``<root>/**/<quarantine>/**``.
_AUTO_QUARANTINE_RE = re.compile(
    r"^/.+/\*\*/" + re.escape(QUARANTINE_DIRNAME) + r"/\*\*$"
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
        validate_only: bool = False,
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


def _reserved_conflicts(content: str, names: Iterable[str]) -> list[str]:
    """Return generated fleet component names already present in the base HCL."""
    return sorted(
        name
        for name in set(names)
        if re.search(rf"\b{re.escape(name)}\b", content)
    )


def _as_path(value: object, label: str) -> str:
    """Validate one absolute live source path/pattern before rendering it."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        raise AlloyConfigError(f"{label} must be an absolute normalized path")
    if len(value.encode("utf-8")) > 4096:
        raise AlloyConfigError(f"{label} is too long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise AlloyConfigError(f"{label} must not contain control characters")
    if "\\" in value or "://" in value:
        raise AlloyConfigError(f"{label} must not contain backslashes or a URI")
    return value


def _is_literal(pattern: str) -> bool:
    return not any(ch in pattern for ch in "*?[")


def _literal_in_quarantine(path: str) -> bool:
    return (
        f"/{QUARANTINE_DIRNAME}/" in path
        or path.endswith(f"/{QUARANTINE_DIRNAME}")
    )


def _exclusion_applies(pattern: str, exclude: str) -> bool:
    """Whether an exclusion can affect one read path.

    The catalog resolver appends a quarantine glob to every managed directory
    root.  A purely literal (non-wildcard) read path outside a quarantine tree
    can never be matched by that automatically generated glob, so it is omitted
    to keep the rendered reader minimal.  Authored exclusions and every
    wildcard path keep their exclusions unchanged.
    """
    if _is_literal(pattern) and _AUTO_QUARANTINE_RE.fullmatch(exclude):
        return _literal_in_quarantine(pattern)
    return True


def _exclusion_field(pattern: str, exclude_paths: Sequence[str]) -> Optional[str]:
    """Render `__path_exclude__` as one validated glob/brace union."""
    applicable: list[str] = []
    for raw in exclude_paths:
        exclude = _as_path(raw, "source exclusion")
        if "{" in exclude or "}" in exclude:
            raise AlloyConfigError("source exclusions must not contain brace characters")
        if _exclusion_applies(pattern, exclude) and exclude not in applicable:
            applicable.append(exclude)
    if not applicable:
        return None
    if len(applicable) == 1:
        return applicable[0]
    return "{" + ",".join(applicable) + "}"


def _multiline_component(source: ResolvedLogSource) -> str:
    """Bounded generated process name for one multiline source chain."""
    digest = hashlib.sha256(source.key.encode("utf-8")).hexdigest()
    return f"fleet_multiline_{digest[:24]}"


def _validate_sources(
    sources: Sequence[ResolvedLogSource],
) -> tuple[list[ResolvedLogSource], list[str]]:
    """Validate deployability, component uniqueness and path ownership."""
    checked: list[ResolvedLogSource] = []
    components: Dict[str, str] = {}
    patterns: list[tuple[str, str]] = []
    names: list[str] = sorted(RESERVED_COMPONENT_NAMES)
    for source in sources:
        if not isinstance(source, ResolvedLogSource):
            raise AlloyConfigError("log sources must be resolved catalogue sources")
        if source.status not in RENDERABLE_SOURCE_STATUSES:
            raise AlloyConfigError(
                f"source {source.key!r} is not deployable (status {source.status!r})"
            )
        if not isinstance(source.component, str) or not _COMPONENT_NAME_RE.match(
            source.component
        ):
            raise AlloyConfigError(
                f"source {source.key!r} has an invalid Alloy component name"
            )
        previous = components.get(source.component)
        if previous is not None:
            raise AlloyConfigError(
                f"component {source.component!r} is claimed by both {previous!r} "
                f"and {source.key!r}"
            )
        components[source.component] = source.key
        names.append(source.component)
        if not source.paths and not source.exact_files:
            raise AlloyConfigError(f"source {source.key!r} declares no live paths")
        for pattern in (*source.paths, *source.exact_files):
            _as_path(pattern, f"source {source.key!r} path")
            if "{" in pattern or "}" in pattern:
                raise AlloyConfigError(
                    f"source {source.key!r} paths must not contain brace characters"
                )
            patterns.append((source.key, pattern))
        if source.multiline is not None:
            names.append(_multiline_component(source))
        checked.append(source)

    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise AlloyConfigError(
            "generated Alloy component name(s) conflict: " + ", ".join(duplicates)
        )
    for index, (first_key, first) in enumerate(patterns):
        for second_key, second in patterns[index + 1 :]:
            if patterns_overlap(first, second):
                raise AlloyConfigError(
                    f"source ownership conflict between {first_key!r} and "
                    f"{second_key!r}: patterns overlap"
                )
    return checked, names


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
    process_component: str = FLEET_PROCESS_COMPONENT,
) -> str:
    lines = [f'loki.source.file "{component}" {{', "  targets = ["]
    for target in targets:
        lines.append("    {")
        for key, value in target:
            lines.append(f"      {key} = {_hcl_string(value)},")
        lines.append("    },")
    lines.append("  ]")
    lines.append(f"  forward_to = [loki.process.{process_component}.receiver]")
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


def _multiline_stanza(component: str, options: MultilineOptions) -> str:
    """Bounded per-source multiline stage feeding the shared pack process."""
    return (
        f'loki.process "{component}" {{\n'
        f"  forward_to = [loki.process.{FLEET_PROCESS_COMPONENT}.receiver]\n"
        "\n"
        "  stage.multiline {\n"
        f"    firstline = {_hcl_string(options.firstline)}\n"
        f"    max_lines = {int(options.max_lines)}\n"
        f'    max_wait_time = "{int(options.max_wait_seconds)}s"\n'
        "  }\n"
        "}"
    )


def _source_targets(
    source: ResolvedLogSource,
    *,
    cluster: str,
    node: str,
    lxc_id: str,
    name: str,
) -> list[list[tuple[str, str]]]:
    """One target map per declared path, then per exact file, in source order."""
    targets: list[list[tuple[str, str]]] = []
    for pattern in (*source.paths, *source.exact_files):
        targets.append(
            _fleet_target(
                path=pattern,
                exclude=_exclusion_field(pattern, source.exclude_paths),
                app=source.app,
                log_kind=source.log_kind,
                cluster=cluster,
                node=node,
                lxc_id=lxc_id,
                name=name,
            )
        )
    return targets


def render_lxc_log_config(
    base: DesiredAlloyConfig,
    *,
    node: str,
    cluster: str,
    lxc_id: str,
    name: str,
    sources: Sequence[ResolvedLogSource],
    retention_hours: int,
) -> DesiredAlloyConfig:
    """Append fleet-managed live file sources to a guest's desired Alloy HCL.

    The base content, journal component names and storage path are preserved
    verbatim; only the resolved source readers, the shared packing process, an
    optional bounded multiline chain per source and the exact resulting content
    hash are added.  Component/ownership conflicts and base configs that
    already use a generated component name are rejected before deployment.
    """
    selected = tuple(sources)
    if not selected:
        return base
    hours = int(retention_hours)
    if hours < 1:
        raise AlloyConfigError("retention_hours must be a positive integer")
    checked, names = _validate_sources(selected)
    conflicts = _reserved_conflicts(base.content, names)
    if conflicts:
        raise AlloyConfigError(
            "desired config already uses reserved fleet component name(s): "
            + ", ".join(conflicts)
        )

    blocks = [_process_stanza()]
    receivers: Dict[str, str] = {}
    for source in checked:
        if source.multiline is None:
            continue
        component = _multiline_component(source)
        receivers[source.key] = component
        blocks.append(_multiline_stanza(component, source.multiline))

    for source in checked:
        blocks.append(
            _source_stanza(
                source.component,
                _source_targets(
                    source,
                    cluster=cluster,
                    node=node,
                    lxc_id=lxc_id,
                    name=name,
                ),
                hours,
                receivers.get(source.key, FLEET_PROCESS_COMPONENT),
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
