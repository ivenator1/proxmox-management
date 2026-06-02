"""GlobalSettings — typed schema for vars.yml.

Gives the driver typed access to every flag it needs. Fields mirror vars.yml keys;
all have safe defaults so a missing vars.yml is not fatal (driver falls back to
running with defaults, which is fine for --check runs).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field


class GlobalSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Kuma / health check (shared across phases)
    kuma_url: str = ""
    kuma_slug: str = ""
    kuma_health_check_retries: int = 5
    kuma_health_check_delay: float = 30.0

    # Fleet-wide flags
    fleet_dry_run: bool = False
    force_window: bool = False

    # custom_update phase settings
    custom_dry_run: bool = False
    custom_allow_reboot: bool = True
    configs_dir: str = "configs"
    host_vars_dir: str = "host_vars"

    # lxc_update phase settings
    lxc_dry_run: bool = False
    lxc_auto_reboot: bool = True
    lxc_unattended: bool = True
    lxc_backup_strategy: str = "snapshot"
    lxc_backup_storage: str = "local"
    lxc_tags: List[str] = Field(default_factory=lambda: ["community-script", "proxmox-helper-scripts"])
    lxc_forks: int = 20
    lxc_continue_on_error: bool = False
    lxc_kuma_map: Dict[str, Any] = Field(default_factory=dict)
    exclude_list: List[str] = Field(default_factory=list)
    os_update_exclude_list: List[str] = Field(default_factory=list)
    snapshot_exclude_list: List[str] = Field(default_factory=list)

    # vm_update phase settings
    vm_dry_run: bool = False
    vm_auto_reboot: bool = True
    vm_backup_strategy: str = "snapshot"
    vm_backup_storage: str = "local"
    vm_kuma_map: Dict[str, Any] = Field(default_factory=dict)
    vm_forks: int = 2

    # remote_host_update phase settings
    remote_dry_run: bool = False
    remote_auto_reboot: bool = True
    remote_pre_update_cmd: str = ""
    remote_kuma_map: Dict[str, Any] = Field(default_factory=dict)
    remote_forks: int = 5

    # Proxmox API credentials (for snapshot operations)
    pve_api_user: str = ""
    pve_api_token_id: str = ""
    pve_api_token_secret: str = ""

    @classmethod
    def load(cls, path: Union[str, Path] = "vars.yml") -> "GlobalSettings":
        """Load from a YAML file. Missing file → all-defaults instance."""
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            raw = {}
        return cls.model_validate(raw)
