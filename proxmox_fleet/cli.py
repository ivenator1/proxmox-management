"""``fleet-update`` entrypoint.

Phase 0 scope: a behaviour-preserving wrapper. By default it runs the existing
``fleet-update.yml`` monolith via ansible-runner, so the new entrypoint is a
drop-in for ``ansible-playbook fleet-update.yml``. Later phases move each phase's
logic into ``proxmox_fleet.flows`` and reduce the YAML to execution primitives.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional


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
    args = parser.parse_args(argv)

    extravars = _parse_extra_vars(args.extra_vars)

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
        playbook="fleet-update.yml",
        inventory=args.inventory,
        extravars=extravars,
        cmdline=" ".join(cmdline_parts) or None,
    )
    return runner.rc if runner.rc is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
