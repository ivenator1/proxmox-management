"""Typed schema for ``configs/<name>.yml`` (the custom_update config files).

This replaces the brittle ``assert`` chain in
``roles/custom_update/tasks/load_config.yml``. Validation runs in the Python
driver *before* any host is touched, so a malformed config fails loudly and
early with a clear message instead of three tasks later disguised as an update
failure.

IMPORTANT (eager-templating boundary): ``UpdateStep.command`` and every
``*_command`` are treated as **opaque literal strings**. We validate that they
are present and non-empty; we never render or interpolate them. Runtime
interpolation of one step's output into a later step's command is done in
``proxmox_fleet.steps`` (in Python), never here and never by Ansible.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _nonempty(value: Optional[str]) -> bool:
    return bool(value is not None and str(value).strip() != "")


class LatestVersion(BaseModel):
    """Source for the 'latest available' version (informational / outdated gate)."""

    model_config = ConfigDict(extra="allow")

    type: Literal["github_release", "command", "none"] = "none"
    repo: str = ""
    command: str = ""

    @model_validator(mode="after")
    def _check_required(self) -> "LatestVersion":
        if self.type == "github_release" and not _nonempty(self.repo):
            raise ValueError("latest_version.type=github_release requires a non-empty 'repo' (owner/name)")
        if self.type == "command" and not _nonempty(self.command):
            raise ValueError("latest_version.type=command requires a non-empty 'command'")
        return self


class ChangedWhen(BaseModel):
    """How to decide whether the system actually changed."""

    model_config = ConfigDict(extra="allow")

    type: Literal["version", "command", "always"] = "version"
    command: str = ""

    @model_validator(mode="after")
    def _check_required(self) -> "ChangedWhen":
        if self.type == "command" and not _nonempty(self.command):
            raise ValueError("changed_when.type=command requires a non-empty 'command'")
        return self


class HealthCheck(BaseModel):
    """Post-update health probe. Failure (after retries) triggers rescue/rollback."""

    model_config = ConfigDict(extra="allow")

    type: Literal["kuma", "command", "http", "none"] = "none"
    kuma_slug: str = ""
    # kuma_monitor_id may be given as int or str in YAML; normalise to str.
    kuma_monitor_id: str = ""
    command: str = ""
    url: str = ""

    @model_validator(mode="before")
    @classmethod
    def _coerce_monitor_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and "kuma_monitor_id" in data and data["kuma_monitor_id"] is not None:
            data = dict(data)
            data["kuma_monitor_id"] = str(data["kuma_monitor_id"])
        return data

    @model_validator(mode="after")
    def _check_required(self) -> "HealthCheck":
        if self.type == "kuma" and not (_nonempty(self.kuma_slug) and _nonempty(self.kuma_monitor_id)):
            raise ValueError("health_check.type=kuma requires both 'kuma_slug' and 'kuma_monitor_id'")
        if self.type == "http" and not _nonempty(self.url):
            raise ValueError("health_check.type=http requires a non-empty 'url'")
        if self.type == "command" and not _nonempty(self.command):
            raise ValueError("health_check.type=command requires a non-empty 'command'")
        return self


class UpdateStep(BaseModel):
    """One entry of ``update_steps``. ``command`` is an opaque literal (never rendered here)."""

    # extra='allow' preserves any future per-step keys so the normalised config
    # passed to the run_shell primitive is non-lossy. populate_by_name lets the
    # 'register' YAML key map to the `register_var` attribute (avoids shadowing
    # a BaseModel attribute).
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    command: str
    when: Optional[Any] = None
    become: bool = False
    chdir: Optional[str] = None
    timeout: Optional[int] = None
    retries: int = 1
    delay: int = 5
    changed_when: Optional[Any] = None
    ignore_errors: bool = False
    environment: Dict[str, Any] = Field(default_factory=dict)
    register_var: Optional[str] = Field(default=None, alias="register")

    @model_validator(mode="after")
    def _check_required(self) -> "UpdateStep":
        if not _nonempty(self.name):
            raise ValueError("update_steps entry needs a non-empty 'name'")
        if not _nonempty(self.command):
            raise ValueError(f"update_steps entry '{self.name}' needs a non-empty 'command'")
        return self


class MaintenanceWindow(BaseModel):
    """Optional maintenance window. Evaluated by proxmox_fleet.window."""

    model_config = ConfigDict(extra="allow")

    days: Optional[List[str]] = None
    start: str = "00:00"
    end: str = "23:59"
    tz: str = "UTC"


class CustomConfig(BaseModel):
    """A fully-validated ``configs/<name>.yml``.

    Mirrors the four asserts in load_config.yml plus the outdated-gate requirement.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    version_command: str = ""
    latest_version: LatestVersion = Field(default_factory=LatestVersion)
    update_only_if_outdated: bool = False
    backup_command: str = ""
    update_steps: List[UpdateStep]
    changed_when: ChangedWhen = Field(default_factory=ChangedWhen)
    health_check: HealthCheck = Field(default_factory=HealthCheck)
    reboot: bool = False
    rollback_command: str = ""

    @model_validator(mode="after")
    def _check_config(self) -> "CustomConfig":
        if not _nonempty(self.name):
            raise ValueError("config must define a non-empty 'name'")
        if not self.update_steps:
            raise ValueError("config must define a non-empty 'update_steps' list")
        if self.update_only_if_outdated:
            if not _nonempty(self.version_command):
                raise ValueError("update_only_if_outdated requires a 'version_command'")
            if self.latest_version.type == "none":
                raise ValueError("update_only_if_outdated requires a latest_version source (type != none)")
        return self
