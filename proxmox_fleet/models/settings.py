"""GlobalSettings — typed schema for vars.yml.

Gives the driver typed access to every flag it needs. Fields mirror vars.yml keys;
all have safe defaults so a missing vars.yml is not fatal (driver falls back to
running with defaults, which is fine for --check runs).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlsplit

import yaml
from pydantic import (  # pyright: ignore[reportMissingImports]
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

#: Shape of a ``--limit`` token: a bare host name/id or one cluster-qualified
#: ``cluster/id`` segment. Mirrors ``web/app.py``'s ``_LIMIT_TOKEN_RE`` (the
#: ``--limit`` tokenizer) so the installer can write ``housekeeping_timer_targets``
#: verbatim into a systemd ``ExecStart``: no whitespace/quoting and no ``%``/``$``
#: for systemd to expand as a specifier or environment variable. Kept local to
#: avoid importing the web layer (which imports this module) from the schema.
_LIMIT_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)?$")


class PveClusterCreds(BaseModel):
    """Per-cluster override for the global ``pve_api_*`` credentials.

    Any field left empty falls back to the matching global setting —
    see :func:`proxmox_fleet.cluster.api_creds`.
    """

    pve_api_user: str = ""
    pve_api_token_id: str = Field(default_factory=str)
    pve_api_token_secret: str = Field(default_factory=str)


class GlobalSettings(BaseModel):
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

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

    # Alloy desired-state enforcement for managed LXC/VM guests.  Ordinary
    # runs opt in with alloy_enabled; --alloy-only overrides that switch.
    alloy_enabled: bool = False
    alloy_config_path: str = "configs/guest.alloy"
    lxc_alloy_exclude_list: List[str] = Field(default_factory=list)
    vm_alloy_exclude_list: List[str] = Field(default_factory=list)

    # Log housekeeping for managed LXCs: bounded, acknowledged import of
    # retained NPM/PBS file-log history into Loki plus short local retention
    # and recurring cache cleanup. Ordinary LXC runs opt in with
    # housekeeping_enabled; --housekeeping-only requests the feature
    # explicitly and re-checks the Loki base via require_loki_url().
    housekeeping_enabled: bool = False
    # Canonical base for manager-side Loki push/query/readiness. Empty means
    # "not configured"; a non-empty value must be an absolute http(s) base
    # without credentials, query, or fragment (see validate_loki_base_url).
    housekeeping_loki_url: str = ""
    # Closed-file cutoff and the desired Alloy journal age limit.
    housekeeping_local_retention_hours: int = Field(default=48, gt=0)
    # Hard local journal disk budget and the journald reserve floor.
    housekeeping_journal_max_mb: int = Field(default=256, gt=0)
    housekeeping_journal_keep_free_mb: int = Field(default=512, gt=0)
    # Cache cleanup cadence, independent of the hourly log tick.
    housekeeping_cache_interval_hours: int = Field(default=24, gt=0)
    # Maximum acknowledged archive payload per guest per run (import resumes).
    housekeeping_backfill_budget_mb: int = Field(default=1024, gt=0)
    lxc_housekeeping_exclude_list: List[str] = Field(default_factory=list)
    # Hourly fleet-housekeeping.timer control. None (the default) keeps the
    # legacy behaviour where the schedule follows housekeeping_enabled; explicit
    # true schedules hourly maintenance even when ordinary fleet housekeeping is
    # off; explicit false always disables the timer, even when
    # housekeeping_enabled is true. Either way that resolves true requests the
    # feature, so a usable housekeeping_loki_url is required.
    housekeeping_timer_enabled: Optional[bool] = None
    # Optional targets for the scheduled run, written as one `--limit` value
    # (bare ids, or cluster-qualified "cluster/id"). Empty keeps the legacy
    # whole-fleet schedule. Entries share the --limit token shape, which keeps
    # the generated systemd ExecStart literal (no quoting and no %/$ to expand).
    housekeeping_timer_targets: List[str] = Field(default_factory=list)

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
    # only when BOTH utilization is high and absolute free space is constrained.
    lxc_disk_warn_percent: int = 75
    # A high-utilization root remains safe enough to update when it has at least
    # this much free space (for example, 20 GiB free on a 200 GiB rootfs).
    lxc_disk_min_free_gb: float = Field(default=10.0, gt=0)
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

    # Read-only manual-adapter settings (scan-tracked hosts; never auto-updated).
    # Scan/reminder path only — deliberately NOT accepted as -e extra vars.
    manual_update_notifications: bool = True
    manual_update_reminder_hours: int = 24
    manual_update_forks: int = 2
    # Per-request timeout for the *_api manual-update adapters. OPNsense
    # firmware/status performs its mirror check synchronously and can exceed
    # the 30s http default; TrueNAS check_available is likewise slow.
    manual_update_api_timeout: float = 120.0

    # node_update / manager phase settings (Phase 2 + Phase 3)
    node_dry_run: bool = False
    node_auto_reboot: bool = True
    manager_lxc_id: str = ""
    apt_proxy_ip: str = ""
    apt_proxy_port: int = 3142

    # Proxmox API credentials (for snapshot operations)
    pve_api_user: str = ""
    pve_api_token_id: str = Field(default_factory=str)
    pve_api_token_secret: str = Field(default_factory=str)
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
        "lxc_alloy_exclude_list",
        "lxc_housekeeping_exclude_list",
        "housekeeping_timer_targets",
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

    @field_validator("housekeeping_timer_targets", mode="after")
    @classmethod
    def _validate_timer_targets(cls, value: List[str]) -> List[str]:
        """Reject targets that cannot become a literal systemd ``--limit`` value.

        The installer writes these into the housekeeping unit's ``ExecStart``.
        Whitespace or quotes would change argument splitting, control characters
        could corrupt the unit file, and ``%``/``$`` would be expanded by systemd
        as specifiers/environment variables. Restricting entries to the same
        bare-or-qualified token shape as ``--limit`` (see ``_LIMIT_TOKEN_RE``)
        keeps the generated argument unambiguous.
        """
        for token in value:
            if not _LIMIT_TOKEN_RE.match(token):
                raise ValueError(
                    f"housekeeping_timer_targets entry {token!r} is not a valid "
                    "id/name token (use bare ids or cluster/id)"
                )
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
    def validate_loki_base_url(cls, value: str) -> str:
        """Validate a manager-side Loki base URL, returning it unchanged.

        Empty is allowed (housekeeping is off by default). A non-empty value
        must be an absolute ``http(s)`` URL with no credentials, query, or
        fragment: it is the canonical base the manager appends fixed
        ``/loki/api/v1/...`` paths to, so credential-bearing or query-carrying
        values are rejected rather than silently misused. Explicit maintenance
        mode (``--housekeeping-only``) calls :meth:`require_loki_url`, which
        delegates here, to enforce the same rules without setting
        ``housekeeping_enabled``.
        """
        if not value:
            return value
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("housekeeping_loki_url has an invalid port") from exc
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or any(ch.isspace() or ord(ch) < 32 for ch in value)
            or (port is not None and port <= 0)
        ):
            raise ValueError("housekeeping_loki_url must be an absolute http(s) base URL")
        if parsed.username is not None or parsed.password is not None or "?" in value or "#" in value:
            raise ValueError("housekeeping_loki_url must not carry credentials, a query, or a fragment")
        return value

    @field_validator("housekeeping_loki_url")
    @classmethod
    def _validate_loki_url(cls, value: str) -> str:
        return cls.validate_loki_base_url(value)

    @model_validator(mode="after")
    def _require_loki_url_when_enabled(self) -> "GlobalSettings":
        """Requesting housekeeping needs a usable URL.

        Both the ordinary opt-in (``housekeeping_enabled``) and an explicitly
        scheduled timer request the feature, so either one requires a configured
        Loki base — even when the other is false.
        """
        if self.housekeeping_enabled or self.housekeeping_timer_enabled is True:
            self.require_loki_url()
        return self

    def require_loki_url(self) -> str:
        """Return the configured Loki base for an explicit housekeeping request.

        Raises ``ValueError`` when housekeeping is requested (via
        ``housekeeping_enabled`` or explicit ``--housekeeping-only`` mode) but
        no usable endpoint is configured. Secrets never live in this value —
        the URL itself carries none.
        """
        if not self.housekeeping_loki_url:
            raise ValueError(
                "housekeeping_loki_url is required when housekeeping is requested"
            )
        return self.validate_loki_base_url(self.housekeeping_loki_url)

    @property
    def housekeeping_timer_effective_enabled(self) -> bool:
        """Whether the hourly fleet-housekeeping timer must be enabled.

        The single decision shared by the installer's install/update
        reconciliation and its summary: an explicit
        ``housekeeping_timer_enabled`` wins, and ``None`` (the default) falls
        back to ``housekeeping_enabled``. Exposed as a property so the installer
        resolves it through ``GlobalSettings.load()`` (``resolve_setting``)
        rather than inferring state from a command's success.
        """
        if self.housekeeping_timer_enabled is not None:
            return self.housekeeping_timer_enabled
        return self.housekeeping_enabled

    @property
    def housekeeping_timer_limit(self) -> str:
        """Comma-joined ``--limit`` value for the scheduled run ("" = whole fleet)."""
        return ",".join(self.housekeeping_timer_targets)

    @classmethod
    def load(cls, path: Union[str, Path] = "vars.yml") -> "GlobalSettings":
        """Load from a YAML file. Missing file → all-defaults instance."""
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            raw = {}
        return cls.model_validate(raw)
