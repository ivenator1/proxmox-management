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
import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

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


def _parse_csv_set(value: Optional[str]) -> Optional[Set[str]]:
    """Parse a comma-separated flag value into a set; None/blank → None (no filter)."""
    if value is None:
        return None
    items = {token.strip() for token in value.split(",") if token.strip()}
    return items or None


# Boolean -e extravars that map 1:1 onto GlobalSettings fields.
_SETTINGS_EXTRAVARS = ("fleet_dry_run", "lxc_verbose", "force_notify", "force_window")

# Per-type record counts shown as table columns, in briefing/phase order.
_HISTORY_COUNT_COLUMNS = ("lxc", "vm", "remote", "node", "custom", "errors", "warnings")


def _format_history_table(rows: List[Dict[str, Any]]) -> str:
    """Render history_summary() rows as a fixed-width table (newest first)."""
    headers = ["TIMESTAMP", "RESULT", *(c.upper() for c in _HISTORY_COUNT_COLUMNS)]
    lines = []
    for row in rows:
        if row["failed"]:
            result = "failed"
        elif row["changed"]:
            result = "changed"
        else:
            result = "clean"
        counts = row.get("counts") or {}
        lines.append([
            str(row["timestamp"]),
            result,
            *(str(counts.get(c, 0)) for c in _HISTORY_COUNT_COLUMNS),
        ])
    widths = [max(len(line[i]) for line in [headers, *lines]) for i in range(len(headers))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
        for line in [headers, *lines]
    )


def history_main(
    *,
    history: Optional[int],
    history_show: Optional[str],
    vars_file: str,
) -> int:
    """Handle ``--history [N]`` / ``--history-show REF`` — read-only, no fleet run.

    Shared by this CLI and the ``fleet-update.py`` wrapper. Reads
    ``fleet_history_dir`` from settings, never imports the driver (so it works
    without ansible-runner installed).
    """
    from proxmox_fleet import history as history_mod
    from proxmox_fleet.models.settings import GlobalSettings

    settings = GlobalSettings.load(vars_file)

    if history_show is not None:
        try:
            run = history_mod.read_run(settings.fleet_history_dir, history_show)
        except FileNotFoundError:
            print(f"no history record {history_show!r} in {settings.fleet_history_dir}")
            return 1
        briefing = run.get("briefing")
        if briefing:
            print(briefing)
        else:
            # Pre-briefing history files: fall back to the raw summary.
            print(json.dumps(run, indent=4, sort_keys=True, ensure_ascii=False))
        return 0

    rows = history_mod.history_summary(settings.fleet_history_dir, limit=history or 0)
    if not rows:
        print(f"no run history found in {settings.fleet_history_dir}")
        return 1
    print(_format_history_table(rows))
    return 0


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
    parser.add_argument("--history", nargs="?", const=10, type=int, default=None, metavar="N",
                        help="show the last N persisted runs (default 10) and exit; no fleet run.")
    parser.add_argument("--history-show", default=None, metavar="TS|latest",
                        help="print one persisted run's briefing (timestamp or 'latest') and exit.")
    parser.add_argument("--limit", default=None, metavar="HOST,ID,...",
                        help="restrict the run to these host names and/or LXC/VM ids "
                             "(everything else is silently skipped).")
    parser.add_argument("--phases", default=None, metavar="P1,P2",
                        help="run only these phases: remote,custom,lxc,vm,node,manager "
                             "(pre-flight and notify always run).")
    parser.add_argument("--scan", action="store_true",
                        help="read-only pending-updates scan (no changes); writes "
                             "pending-*.json to fleet_history_dir and exits.")
    args = parser.parse_args(argv)

    if args.history is not None or args.history_show is not None:
        return history_main(history=args.history, history_show=args.history_show,
                            vars_file=args.vars_file)

    if args.scan:
        from proxmox_fleet import scan as scan_mod
        from proxmox_fleet.models.settings import GlobalSettings as _Settings

        return scan_mod.run_fleet_scan(
            settings=_Settings.load(args.vars_file),
            inventory_path=args.inventory,
            limit=_parse_csv_set(args.limit),
        )

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
        limit=_parse_csv_set(args.limit),
        phases=_parse_csv_set(args.phases),
    )


if __name__ == "__main__":
    raise SystemExit(main())
