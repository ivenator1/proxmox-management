"""``fleet-update`` entrypoint.

Phase 0 scope: a behaviour-preserving wrapper. By default it runs the existing
``fleet-update.yml`` monolith via ansible-runner, so the new entrypoint is a
drop-in for ``ansible-playbook fleet-update.yml``. Later phases move each phase's
logic into ``proxmox_fleet.flows`` and reduce the YAML to execution primitives.

Phase 2-wire: ``--use-custom-flow`` routes Phase 0b through the Python driver
before handing off to the playbook (with skip_phase_0b=true so the Ansible
role is not also executed).

Phase 3-wire: ``--use-lxc-flow`` routes Phase 1 (LXC updates) through the
Python driver (skip_phase_1=true tells the playbook to skip its Phase 1 block).

Both imports are lazy so unit tests never need ansible-runner.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

_CUSTOM_STATE_PATH = "/tmp/fleet_custom_state.json"
_LXC_STATE_PATH = "/tmp/fleet_lxc_state.json"


def _parse_extra_vars(pairs: List[str]) -> dict:
    out: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"-e expects key=value, got: {pair!r}")
        key, val = pair.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet-update", description="Proxmox fleet updater (Python control plane).")
    parser.add_argument("--check", action="store_true", help="dry run (no changes).")
    parser.add_argument("--limit", default=None, help="limit execution to a host/group.")
    parser.add_argument("-e", "--extra-vars", action="append", default=[], metavar="KEY=VALUE",
                        help="extra vars passed through to the play(s).")
    parser.add_argument("--inventory", default="hosts.ini", help="inventory path.")
    parser.add_argument(
        "--use-custom-flow",
        action="store_true",
        default=False,
        help="Route Phase 0b through flows/custom.py (Python driver) instead of the custom_update role.",
    )
    parser.add_argument(
        "--use-lxc-flow",
        action="store_true",
        default=False,
        help="Route Phase 1 (LXC updates) through flows/lxc.py (Python driver) instead of the lxc_update role.",
    )
    parser.add_argument(
        "--vars-file",
        default="vars.yml",
        help="Path to vars.yml used by --use-custom-flow / --use-lxc-flow to load GlobalSettings.",
    )
    args = parser.parse_args(argv)

    extravars = _parse_extra_vars(args.extra_vars)

    # Phase 2-wire: run Phase 0b in Python, then tell the playbook to skip it.
    if args.use_custom_flow or args.use_lxc_flow:
        from proxmox_fleet import driver  # lazy: avoids ansible-runner import in unit tests
        from proxmox_fleet.models.settings import GlobalSettings

        settings = GlobalSettings.load(args.vars_file)

        # Propagate CLI extravars that affect driver behaviour into settings.
        if extravars.get("fleet_dry_run", "").lower() in ("true", "1", "yes"):
            settings = settings.model_copy(update={"fleet_dry_run": True})

        if args.use_custom_flow:
            driver.run_custom_phase(
                settings=settings,
                inventory_path=args.inventory,
                extra_vars=extravars,
                check=args.check,
                state_output_path=_CUSTOM_STATE_PATH,
            )
            extravars["skip_phase_0b"] = "true"
            extravars["fleet_custom_state_path"] = _CUSTOM_STATE_PATH

        # Phase 3-wire: run Phase 1 (LXC) in Python, then tell the playbook to skip it.
        if args.use_lxc_flow:
            driver.run_lxc_phase(
                settings=settings,
                inventory_path=args.inventory,
                check=args.check,
                state_output_path=_LXC_STATE_PATH,
            )
            extravars["skip_phase_1"] = "true"
            extravars["fleet_lxc_state_path"] = _LXC_STATE_PATH

    try:
        import ansible_runner  # lazy: not needed for unit tests
    except ImportError:
        print("ansible-runner is not installed. Install with: pip install 'proxmox-fleet[runner]'",
              file=sys.stderr)
        return 2

    cmdline_parts: List[str] = []
    if args.check:
        cmdline_parts.append("--check")
    if args.limit:
        cmdline_parts.extend(["--limit", args.limit])

    runner = ansible_runner.run(
        project_dir=os.getcwd(),
        playbook="fleet-update.yml",
        inventory=str(Path(args.inventory).resolve()),
        extravars=extravars,
        cmdline=" ".join(cmdline_parts) or None,
    )
    return runner.rc if runner.rc is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
