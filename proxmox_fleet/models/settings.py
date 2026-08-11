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


class PveClusterCreds(BaseModel):
    """Per-cluster override for the global ``pve_api_*`` credentials.

    Any field left empty falls back to the matching global setting —
    see :func:`proxmox_fleet.cluster.api_creds`.
    """

    pve_api_user: str = ""
    pve_api_token_id: str = ""
    pve_api_token_secret: str = ""


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
    # Warn below the community scripts' own >80% abort (check_container_storage)
    # so a container is flagged a run or two before its updates start failing.
    lxc_disk_warn_percent: int = 75
    # Temporarily raise cores/memory to the ct script's var_cpu/var_ram for the
    # update, then restore. Defaults off: upstream dropped build-time scaling, so
    # turning this on adds `pct set` calls the scripts themselves no longer make.
    lxc_resource_scaling: bool = False
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
    # Optional per-cluster overrides, keyed by cluster name — see
    # proxmox_fleet.cluster.api_creds() for the per-field fallback rules.
    pve_clusters: Dict[str, PveClusterCreds] = Field(default_factory=dict)

    # Timeouts & retries (formerly hardcoded)
    apt_proxy_check_timeout: float = 30.0
    node_reboot_port_wait_timeout: float = 300.0
    snapshot_retries: int = 3
    snapshot_retry_delay: float = 15.0
    # community.proxmox defaults its overall snapshot wait to 30s and each API
    # request to 5s, which is too short for large disks or slow storage.
    snapshot_timeout: int = 600
    snapshot_api_timeout: int = 30
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
    # How many of the NEWEST run files keep their per-record `packages` detail
    # (the exact OS package lists, PR1). Older timestamped runs are stripped in
    # place by history._strip_package_detail; latest.json and totals.json are
    # never touched. <=0 → never strip (keep all detail).
    fleet_package_detail_keep: int = 7
    scan_history_keep: int = 30
    force_notify: bool = False

    # Web dashboard (fleet-dashboard)
    dashboard_host: str = "0.0.0.0"  # nosec B104 - LAN-facing homelab dashboard by design
    dashboard_port: int = 8421

    @field_validator("canary_hosts", mode="before")
    @classmethod
    def _stringify_canary_hosts(cls, value: Any) -> Any:
        """Coerce entries to str so integer vmids in vars.yml are accepted."""
        if isinstance(value, list):
            return [str(v) for v in value]
        return value

    @field_validator(
        "exclude_list",
        "os_update_exclude_list",
        "app_update_exclude_list",
        "snapshot_exclude_list",
        "os_only_lxc_list",
        mode="before",
    )
    @classmethod
    def _stringify_id_list(cls, value: Any) -> Any:
        """Coerce entries to str so integer/qualified ids in vars.yml are accepted.

        YAML writes ``exclude_list: [103, "alpha/110"]`` with a mixed
        int/str list, which would otherwise fail validation (the fields are
        ``List[str]``). Mirrors ``_stringify_canary_hosts``.
        """
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
