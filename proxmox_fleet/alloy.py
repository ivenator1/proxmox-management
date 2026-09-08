"""Desired-state compliance for Grafana Alloy on managed LXC and VM guests.

Python owns drift classification and reconciliation policy.  The executor only
runs the direct-guest or pct-mediated probe/deploy primitives selected here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from proxmox_fleet.runner import PrimitiveResult


PLACEHOLDER_MARKER = "REPLACE_WITH_LOKI_ENDPOINT"


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
