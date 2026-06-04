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
from typing import List, Optional


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
    if _is_true(extravars.get("fleet_dry_run", "")):
        settings = settings.model_copy(update={"fleet_dry_run": True})
    if _is_true(extravars.get("lxc_verbose", "")):
        settings = settings.model_copy(update={"lxc_verbose": True})
    if _is_true(extravars.get("force_notify", "")):
        settings = settings.model_copy(update={"force_notify": True})
    if _is_true(extravars.get("force_window", "")):
        settings = settings.model_copy(update={"force_window": True})

    return driver.run_fleet(
        settings=settings,
        inventory_path=args.inventory,
        check=args.check,
        extra_vars=extravars,
    )


if __name__ == "__main__":
    raise SystemExit(main())
