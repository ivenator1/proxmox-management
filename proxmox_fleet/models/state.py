"""Typed fleet-state records — the replacement for the ``fleet_*_data`` fact lists.

Field shapes match the dicts appended today by ``tasks/fleet-state-append.yml``
and each role's ``report.yml``, so a harvested ``raw-state.json`` / ``latest.json``
deserialises directly and the briefing renders identically.
"""

from __future__ import annotations

import json
import math
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HousekeepingSummary(BaseModel):
    """Per-guest log-housekeeping outcome for one run.

    Set on an :class:`LxcRecord` only when housekeeping did something worth
    reporting (reclamation, pending backfill, block, audit finding, or a
    logging-retention configuration change); an omitted summary keeps idle
    records byte-identical to legacy ones. ``status`` mirrors the policy's
    classification in ``proxmox_fleet.housekeeping``.
    """

    model_config = ConfigDict(extra="allow")

    status: Literal["Configured", "Cleaned", "Backfill pending", "Blocked", "Audit"]
    bytes_reclaimed: int = Field(default=0, ge=0)
    bytes_archived: int = Field(default=0, ge=0)
    files_pruned: int = Field(default=0, ge=0)


_DISK_DETAIL_LIMIT = 160
# Control/format/line-separator characters must never reach a rendered warning.
_UNSAFE_TEXT_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _disk_count(value: object, field: str) -> int:
    """Validate a bounded nonnegative integer element of a disk-usage record."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a nonnegative integer")
    if value < 0:
        raise ValueError(f"{field} must be nonnegative")
    return value


def _disk_share(value: object, field: str) -> float:
    """Validate a finite percentage in [0, 100] (booleans rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number between 0 and 100")
    number = float(value)
    if not math.isfinite(number) or not (0.0 <= number <= 100.0):
        raise ValueError(f"{field} must be a finite number between 0 and 100")
    return number


class LxcRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    node: str
    name: str
    id: str
    app: str
    os: str = ""
    snap: bool = True
    # Exact OS packages upgraded this run (PR1): [{"name", "from", "to"}].
    # None keeps idle/dry-run/old records key-free; only real-run success
    # records carry detail (see flows.lxc).
    packages: Optional[List[Dict[str, str]]] = Field(default=None, exclude_if=lambda value: value is None)
    alloy: Optional[str] = Field(default=None, exclude_if=lambda value: value is None)
    # Log-housekeeping outcome — omitted entirely when None, preserving the
    # legacy record byte shape for guests that did nothing relevant.
    housekeeping: Optional[HousekeepingSummary] = Field(
        default=None, exclude_if=lambda value: value is None
    )


class VmRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    node: str
    vmid: str
    name: str
    status: str
    pkg_count: Optional[int] = None
    packages: Optional[List[Dict[str, str]]] = Field(default=None, exclude_if=lambda value: value is None)
    alloy: Optional[str] = Field(default=None, exclude_if=lambda value: value is None)


class RemoteRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    host: str
    status: str
    pkg_count: Optional[int] = None
    packages: Optional[List[Dict[str, str]]] = Field(default=None, exclude_if=lambda value: value is None)


class NodeRecord(BaseModel):
    """Covers both 'node' and 'manager' record types (same shape)."""

    model_config = ConfigDict(extra="allow")
    node: str
    status: str
    pkg_count: Optional[int] = None
    packages: Optional[List[Dict[str, str]]] = Field(default=None, exclude_if=lambda value: value is None)
    # Node/manager status strings intentionally have no dry-run variant;
    # persist this marker so the ledger never treats simulation as applied.
    dry_run: Optional[bool] = Field(default=None, exclude_if=lambda value: value is None)
    # Why this node needs (or needed) a reboot — ordinary marker/kernel reasons
    # plus NVIDIA target/loaded mismatch reasons. Omitted entirely when the
    # node does not need a reboot, preserving the legacy record byte shape.
    reboot_reasons: Optional[List[str]] = Field(default=None, exclude_if=lambda value: value is None)
    # Structured post-upgrade results (target-kernel NVIDIA readiness + classification
    # for nvidia_host nodes). Omitted entirely when no checks ran, so legacy
    # records and non-nvidia hosts stay byte-identical.
    checks: Optional[Dict[str, Any]] = Field(default=None, exclude_if=lambda value: value is None)


class CustomRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    host: str
    name: str
    app: str


class ErrorEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    host: str
    task: str
    error: str


class DiskUsageEntry(BaseModel):
    """One bounded root-filesystem consumer in a disk-usage insight.

    ``allocated_bytes`` is allocated space (``st_blocks * 512``), never apparent
    length or file contents. ``share_percent`` is a share of the guest's used
    root-filesystem bytes, so it can never exceed 100. Metadata keeps the full
    measured path; only the rendered warning clips/sanitizes it.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    path: str
    allocated_bytes: int
    share_percent: float
    max_expected_share_percent: float
    # "directory-other" is the residual of a parent after crediting retained
    # descendants; "root-files" is the direct-files bucket below "/".
    kind: Literal["directory", "directory-other", "root-files"] = "directory"

    @field_validator("allocated_bytes", mode="before")
    @classmethod
    def _validate_allocated_bytes(cls, value: object) -> int:
        return _disk_count(value, "allocated_bytes")

    @field_validator("share_percent", mode="before")
    @classmethod
    def _validate_share(cls, value: object) -> float:
        return _disk_share(value, "share_percent")

    @field_validator("max_expected_share_percent", mode="before")
    @classmethod
    def _validate_expected_share(cls, value: object) -> float:
        return _disk_share(value, "max_expected_share_percent")


class DiskUsageInsight(BaseModel):
    """Structured, bounded disk-usage enrichment attached to a disk warning."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    status: Literal["complete", "partial", "unavailable"]
    # Defaults to None so exclude_none serialization (history/dashboard) cannot
    # drop the key and then fail to reload an unavailable insight.
    used_bytes: Optional[int] = None
    entries: List[DiskUsageEntry] = Field(max_length=20)
    omitted: int = 0
    detail: str = ""

    @field_validator("used_bytes", mode="before")
    @classmethod
    def _validate_used_bytes(cls, value: object) -> Optional[int]:
        if value is None:
            return None
        return _disk_count(value, "used_bytes")

    @field_validator("omitted", mode="before")
    @classmethod
    def _validate_omitted(cls, value: object) -> int:
        return _disk_count(value, "omitted")

    @field_validator("detail", mode="before")
    @classmethod
    def _validate_detail(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("detail must be a string")
        if len(value) > _DISK_DETAIL_LIMIT:
            raise ValueError(
                f"detail must be at most {_DISK_DETAIL_LIMIT} characters"
            )
        if any(
            unicodedata.category(char) in _UNSAFE_TEXT_CATEGORIES for char in value
        ):
            raise ValueError("detail must not contain control characters")
        return value

    def with_detail(self, detail: str) -> "DiskUsageInsight":
        """Return a re-validated copy carrying ``detail``.

        Pydantic's ``model_copy(update=...)`` skips validation, so callers that
        append a policy finding must use this helper rather than bypassing the
        bounded/sanitized detail contract.
        """
        return DiskUsageInsight.model_validate({**self.model_dump(), "detail": detail})


class WarningEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    host: str
    task: str
    warning: str
    notifying: Optional[bool] = Field(default=None, exclude_if=lambda value: value is None)
    # Structured disk-usage enrichment for the existing lack-of-space warning.
    # Omitted entirely when None so legacy records keep their exact byte shape.
    disk_usage: Optional[DiskUsageInsight] = Field(
        default=None, exclude_if=lambda value: value is None
    )


class FleetState(BaseModel):
    """The whole accumulated run state. Replaces the localhost fact lists."""

    model_config = ConfigDict(extra="allow")

    lxc: List[LxcRecord] = Field(default_factory=list)
    vm: List[VmRecord] = Field(default_factory=list)
    remote: List[RemoteRecord] = Field(default_factory=list)
    node: List[NodeRecord] = Field(default_factory=list)
    custom: List[CustomRecord] = Field(default_factory=list)
    errors: List[ErrorEntry] = Field(default_factory=list)
    warnings: List[WarningEntry] = Field(default_factory=list)
    changed: bool = False
    failed: bool = False
    housekeeping_attempted: bool = Field(default=False, exclude=True)

    @classmethod
    def from_raw(cls, data: dict) -> "FleetState":
        """Build from a harvested raw-state dict (keys may use the fleet_* names)."""
        # Accept both the fleet_*_data names (from the YAML dump) and the short names.
        alias = {
            "fleet_lxc_data": "lxc",
            "fleet_vm_data": "vm",
            "fleet_remote_data": "remote",
            "fleet_node_data": "node",
            "fleet_custom_data": "custom",
            "fleet_error_log": "errors",
            "fleet_warning_log": "warnings",
            "fleet_changed": "changed",
            "fleet_failed": "failed",
        }
        normalised = {alias.get(k, k): v for k, v in data.items()}
        return cls.model_validate(normalised)

    @classmethod
    def load(cls, path: Union[str, Path]) -> "FleetState":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return cls.from_raw(json.load(fh))
        except (OSError, ValueError):
            # State files are authoritative inputs here; callers need the
            # original failure rather than a silently fabricated empty state.
            raise

    def dump(self, path: Union[str, Path]) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.model_dump(), fh, indent=2, ensure_ascii=False)
        except OSError:
            raise

    def dump_for_ansible(self, path: Union[str, Path]) -> None:
        """Write with fleet_* key names so an Ansible play can include_vars the
        JSON as localhost facts. Consumed by the molecule harness' verify.yml
        (mol_run_flow.py writes it; verify.yml asserts on fleet_lxc_data etc.)."""
        alias = {
            "lxc": "fleet_lxc_data",
            "vm": "fleet_vm_data",
            "remote": "fleet_remote_data",
            "node": "fleet_node_data",
            "custom": "fleet_custom_data",
            "errors": "fleet_error_log",
            "warnings": "fleet_warning_log",
            "changed": "fleet_changed",
            "failed": "fleet_failed",
        }
        data = {alias[k]: v for k, v in self.model_dump().items() if k in alias}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
        except OSError:
            raise
