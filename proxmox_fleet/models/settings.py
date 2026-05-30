"""GlobalSettings — typed schema for vars.yml.

Gives the driver typed access to every flag it needs. Fields mirror vars.yml keys;
all have safe defaults so a missing vars.yml is not fatal (driver falls back to
running with defaults, which is fine for --check runs).
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

import yaml
from pydantic import BaseModel, ConfigDict


class GlobalSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    kuma_url: str = ""
    kuma_health_check_retries: int = 5
    kuma_health_check_delay: float = 30.0
    custom_dry_run: bool = False
    fleet_dry_run: bool = False
    custom_allow_reboot: bool = True
    force_window: bool = False
    configs_dir: str = "configs"
    host_vars_dir: str = "host_vars"

    @classmethod
    def load(cls, path: Union[str, Path] = "vars.yml") -> "GlobalSettings":
        """Load from a YAML file. Missing file → all-defaults instance."""
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            raw = {}
        return cls.model_validate(raw)
