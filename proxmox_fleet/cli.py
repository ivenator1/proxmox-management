"""``fleet-update`` entrypoint — the Python control plane.

The driver owns the whole run end-to-end: ``driver.run_fleet()`` performs the
pre-flight apt-proxy check, executes every phase (remote → custom → lxc → vm →
node+manager) via the typed flows, and renders/dispatches the final briefing.
Ansible is invoked only as single-purpose execution primitives through
``RunnerExecutor`` (which needs ``ansible-runner`` installed).

Imports are lazy so unit tests never need ansible-runner.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from proxmox_fleet.models.settings import GlobalSettings


def _parse_extra_vars(pairs: List[str]) -> dict:
    out: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"-e expects key=value, got: {pair!r}")
        key, val = pair.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def _is_true(val: str) -> bool:
    return val.lower() in ("true", "1", "yes")


# Boolean -e extravars that map 1:1 onto GlobalSettings fields.
_SETTINGS_EXTRAVARS = ("fleet_dry_run", "lxc_verbose", "force_notify", "force_window")


def apply_extravar_overrides(
    settings: "GlobalSettings", extravars: Dict[str, str]
) -> "GlobalSettings":
    """Fold boolean ``-e`` extravars that affect driver behaviour into settings.

    Shared by this CLI and the ``fleet-update.py`` wrapper so the two entry
    points cannot drift on which flags they propagate.
    """
    updates = {
        key: True
        for key in _SETTINGS_EXTRAVARS
        if _is_true(str(extravars.get(key, "")))
    }
    return settings.model_copy(update=updates) if updates else settings


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fleet-update",
        description="Proxmox fleet updater (Python control plane).",
    )
    parser.add_argument("--check", action="store_true", help="dry run (no changes).")
    parser.add_argument("-e", "--extra-vars", action="append", default=[], metavar="KEY=VALUE",
                        help="extra vars (e.g. fleet_dry_run=true, force_notify=true).")
    parser.add_argument("--inventory", default="hosts.ini", help="inventory path.")
    parser.add_argument("--vars-file", default="vars.yml",
                        help="path to vars.yml used to load GlobalSettings.")
    args = parser.parse_args(argv)

    extravars = _parse_extra_vars(args.extra_vars)

    try:
        from proxmox_fleet import driver
        from proxmox_fleet.models.settings import GlobalSettings
    except ImportError as exc:  # pragma: no cover - defensive
        raise SystemExit(f"failed to import the driver: {exc}")

    settings = GlobalSettings.load(args.vars_file)

    # Propagate CLI extravars that affect driver behaviour into settings.
    settings = apply_extravar_overrides(settings, extravars)

    return driver.run_fleet(
        settings=settings,
        inventory_path=args.inventory,
        check=args.check,
        extra_vars=extravars,
    )


if __name__ == "__main__":
    raise SystemExit(main())
