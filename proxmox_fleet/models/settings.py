"""GlobalSettings — typed schema for vars.yml.

Gives the driver typed access to every flag it needs. Fields mirror vars.yml keys;
all have safe defaults so a missing vars.yml is not fatal (driver falls back to
running with defaults, which is fine for --check runs).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    # Canary / staged rollout (remote, lxc, and vm phases). Canary hosts —
    # names/vmids listed here, or hosts with `canary: true` in inventory/
    # host_vars — update first; the rest of the phase runs only if no canary
    # failed and (after the soak window) every Kuma-monitored canary is healthy.
    canary_hosts: List[str] = Field(default_factory=list)
    canary_soak_minutes: float = 0.0

    # custom_update phase settings
    custom_dry_run: bool = False
    custom_allow_reboot: bool = True
    configs_dir: str = "configs"
    host_vars_dir: str = "host_vars"

    # lxc_update phase settings
    lxc_dry_run: bool = False
    lxc_auto_reboot: bool = True
    lxc_unattended: bool = True
    lxc_verbose: bool = False
    lxc_backup_strategy: str = "snapshot"
    lxc_backup_storage: str = "local"
    lxc_tags: List[str] = Field(default_factory=lambda: ["community-script", "proxmox-helper-scripts"])
    lxc_forks: int = 20
    lxc_continue_on_error: bool = False
    lxc_kuma_map: Dict[str, Any] = Field(default_factory=dict)
    exclude_list: List[str] = Field(default_factory=list)
    os_update_exclude_list: List[str] = Field(default_factory=list)
    app_update_exclude_list: List[str] = Field(default_factory=list)
    snapshot_exclude_list: List[str] = Field(default_factory=list)
    os_only_lxc_list: List[str] = Field(default_factory=list)

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

    # node_update / manager phase settings (Phase 2 + Phase 3)
    node_dry_run: bool = False
    node_auto_reboot: bool = True
    manager_lxc_id: str = ""
    apt_proxy_ip: str = ""
    apt_proxy_port: int = 3142

    # Proxmox API credentials (for snapshot operations)
    pve_api_user: str = ""
    pve_api_token_id: str = ""
    pve_api_token_secret: str = ""

    # Timeouts & retries (formerly hardcoded)
    apt_proxy_check_timeout: float = 30.0
    node_reboot_port_wait_timeout: float = 300.0
    snapshot_retries: int = 3
    snapshot_retry_delay: float = 15.0
    notifier_retries: int = 15
    deadmans_retries: int = 5
    node_apt_retries: int = 5
    node_apt_retry_delay: float = 30.0

    # Phase 4 — briefing / history / notifiers
    # notifiers defaults to None (not []) so an unset value is distinguishable
    # from an explicit empty list, matching the Ansible `notifiers is defined` shim.
    notifiers: Optional[List[Dict[str, Any]]] = None
    discord_webhook: str = ""
    fleet_deadmans_url: str = ""
    fleet_history_enabled: bool = True
    fleet_history_dir: str = "/var/log/fleet-update"
    fleet_history_keep: int = 30
    scan_history_keep: int = 30
    force_notify: bool = False

    # Web dashboard (fleet-dashboard)
    dashboard_host: str = "0.0.0.0"  # nosec B104 - LAN-facing homelab dashboard by design
    dashboard_port: int = 8421
    dashboard_token: str = ""        # empty = run-trigger endpoint unauthenticated

    @field_validator("canary_hosts", mode="before")
    @classmethod
    def _stringify_canary_hosts(cls, value: Any) -> Any:
        """Coerce entries to str so integer vmids in vars.yml are accepted."""
        if isinstance(value, list):
            return [str(v) for v in value]
        return value

    @field_validator("lxc_kuma_map", "vm_kuma_map", "remote_kuma_map", mode="before")
    @classmethod
    def _stringify_kuma_keys(cls, value: Any) -> Any:
        """Coerce kuma-map keys to str so integer vmids in vars.yml are accepted.

        YAML writes ``lxc_kuma_map: {101: 5}`` with an int key, which would
        otherwise fail validation (the field is ``Dict[str, Any]``).
        """
        if isinstance(value, dict):
            return {str(k): v for k, v in value.items()}
        return value

    @classmethod
    def load(cls, path: Union[str, Path] = "vars.yml") -> "GlobalSettings":
        """Load from a YAML file. Missing file → all-defaults instance."""
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            raw = {}
        return cls.model_validate(raw)
