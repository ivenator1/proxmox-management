"""Typed fleet-state records — the replacement for the ``fleet_*_data`` fact lists.

Field shapes match the dicts appended today by ``tasks/fleet-state-append.yml``
and each role's ``report.yml``, so a harvested ``raw-state.json`` / ``latest.json``
deserialises directly and the briefing renders identically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Union

from pydantic import BaseModel, ConfigDict, Field


class LxcRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    node: str
    name: str
    id: str
    app: str
    os: str = ""
    snap: bool = True


class VmRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    node: str
    vmid: str
    name: str
    status: str


class RemoteRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    host: str
    status: str


class NodeRecord(BaseModel):
    """Covers both 'node' and 'manager' record types (same shape)."""

    model_config = ConfigDict(extra="allow")
    node: str
    status: str


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


class WarningEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    host: str
    task: str
    warning: str


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
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_raw(json.load(fh))

    def dump(self, path: Union[str, Path]) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.model_dump(), fh, indent=2, ensure_ascii=False)
