"""Pydantic schemas for configs (configs/<name>.yml) and fleet state records."""

from proxmox_fleet.models.config import (
    ChangedWhen,
    CustomConfig,
    HealthCheck,
    LatestVersion,
    MaintenanceWindow,
    UpdateStep,
)
from proxmox_fleet.models.state import (
    CustomRecord,
    ErrorEntry,
    FleetState,
    LxcRecord,
    NodeRecord,
    RemoteRecord,
    VmRecord,
    WarningEntry,
)

__all__ = [
    "ChangedWhen",
    "CustomConfig",
    "HealthCheck",
    "LatestVersion",
    "MaintenanceWindow",
    "UpdateStep",
    "CustomRecord",
    "ErrorEntry",
    "FleetState",
    "LxcRecord",
    "NodeRecord",
    "RemoteRecord",
    "VmRecord",
    "WarningEntry",
]
