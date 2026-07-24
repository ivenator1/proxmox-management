#!/usr/bin/env python3
"""fleet-update.py — human-friendly wrapper for the Proxmox fleet updater.

Auto-bootstraps into .venv if present: if .venv/bin/python exists at the repo
root and the current interpreter is not already that one, re-execs into it via
os.execv (replaces the process image — no child process created).

After the venv jump, parses friendly flags and calls driver.run_fleet() directly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Venv bootstrap — must run before any proxmox_fleet imports
# ---------------------------------------------------------------------------

def _bootstrap_venv() -> None:
    repo_root = Path(__file__).resolve().parent
    venv_dir = repo_root / ".venv"
    venv_python = venv_dir / "bin" / "python"
    if venv_python.exists():
        # Compare sys.prefix, not realpath(sys.executable): venvs created
        # without --copies symlink to the base interpreter, so the venv
        # python and the system python can share the same realpath while
        # having different sys.prefix (and thus different site-packages).
        if Path(sys.prefix).resolve() != venv_dir.resolve():
            venv_str = str(venv_python)
            os.execv(venv_str, [venv_str] + sys.argv)
    else:
        try:
            import proxmox_fleet  # noqa: F401
        except ImportError:
            print(
                "WARNING: .venv not found and proxmox_fleet is not importable. "
                "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e .",
                file=sys.stderr,
            )


_bootstrap_venv()


# ---------------------------------------------------------------------------
# After venv bootstrap — proxmox_fleet is now importable
# ---------------------------------------------------------------------------

import argparse  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="./fleet-update.py",
        description="Proxmox fleet updater — human-friendly interface.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
  # Dry run with forced notification (safe — no real changes made):
  ./fleet-update.py --dry-run --force-notify

  # Full live run bypassing maintenance windows:
  ./fleet-update.py --force-window

  # Verbose LXC output with a custom inventory file:
  ./fleet-update.py --verbose --inventory /etc/fleet/hosts.ini

  # Pass a raw extra var (same as old 'fleet-update -e KEY=VALUE'). Only
  # fleet_dry_run, lxc_verbose, force_notify, force_window and custom_dry_run
  # are honoured -- any other key is accepted and silently ignored:
  ./fleet-update.py -e custom_dry_run=true

  # Show the last 5 persisted runs / replay the latest run's briefing:
  ./fleet-update.py --history 5
  ./fleet-update.py --history-show latest

  # Re-run a single failed LXC (dry-run first), or only the VM phase:
  ./fleet-update.py --dry-run --phases lxc --limit 105
  ./fleet-update.py --phases vm --limit media-vm
""",
    )


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run", "--check",
        dest="dry_run",
        action="store_true",
        help=(
            "Simulate the run: no packages installed, no reboots, no snapshots. "
            "Sets both check=True (runner level) and fleet_dry_run=True (Python level). "
            "A notification is still sent when --force-notify is also given."
        ),
    )
    parser.add_argument(
        "--force-notify", "--notify",
        dest="force_notify",
        action="store_true",
        help="Send a Discord/ntfy notification even if nothing changed.",
    )
    parser.add_argument(
        "--verbose",
        dest="lxc_verbose",
        action="store_true",
        help="Enable verbose output for LXC container updates.",
    )
    parser.add_argument(
        "--force-window",
        dest="force_window",
        action="store_true",
        help="Bypass maintenance-window checks for all hosts.",
    )
    parser.add_argument(
        "-e", "--extra-vars",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Raw extra var, repeatable. Only fleet_dry_run, lxc_verbose, force_notify, "
             "force_window and custom_dry_run are honoured; any other key is accepted "
             "and silently ignored (set it in vars.yml instead).",
    )
    parser.add_argument(
        "--inventory",
        default="hosts.ini",
        metavar="PATH",
        help="Path to Ansible inventory file (default: hosts.ini).",
    )
    parser.add_argument(
        "--vars-file",
        default="vars.yml",
        metavar="PATH",
        help="Path to vars.yml used to load GlobalSettings (default: vars.yml).",
    )
    parser.add_argument(
        "--history",
        nargs="?",
        const=10,
        type=int,
        default=None,
        metavar="N",
        help="Show the last N persisted runs (default: 10) and exit — no fleet run.",
    )
    parser.add_argument(
        "--history-show",
        default=None,
        metavar="TS|latest",
        help="Print one persisted run's briefing (run timestamp or 'latest') and exit.",
    )
    parser.add_argument(
        "--limit",
        default=None,
        metavar="HOST,ID,...",
        help=(
            "Restrict the run to these host names and/or LXC/VM ids — everything else "
            "is silently skipped. Use cluster/ID (e.g. alpha/101) to target one "
            "cluster's container/VM in a multi-cluster fleet. Use 'manager' to include "
            "the manager self-update."
        ),
    )
    parser.add_argument(
        "--phases",
        default=None,
        metavar="P1,P2",
        help=(
            "Run only these phases: remote,custom,lxc,vm,node,manager. "
            "Pre-flight and the final notify/history phase always run."
        ),
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help=(
            "Read-only pending-updates scan: per-host pending OS packages plus "
            "community-script app versions (current → latest) for managed LXCs. "
            "Writes pending-*.json next to the run history; no changes are made."
        ),
    )


def main() -> int:
    parser = _build_parser()
    _add_arguments(parser)
    args = parser.parse_args()

    try:
        from proxmox_fleet import driver
        from proxmox_fleet.cli import (
            _parse_csv_set,
            _parse_extra_vars,
            apply_extravar_overrides,
            history_main,
            run_locked,
        )
        from proxmox_fleet.models.settings import GlobalSettings
    except ImportError as exc:
        raise SystemExit(f"Cannot import proxmox_fleet — is the venv active? ({exc})")

    if args.history is not None or args.history_show is not None:
        return history_main(history=args.history, history_show=args.history_show,
                            vars_file=args.vars_file)

    if args.scan:
        from proxmox_fleet import scan as scan_mod

        scan_settings = GlobalSettings.load(args.vars_file)
        return run_locked(scan_settings, lambda: scan_mod.run_fleet_scan(
            settings=scan_settings,
            inventory_path=args.inventory,
            limit=_parse_csv_set(args.limit),
        ))

    extravars = _parse_extra_vars(args.extra_vars)

    settings = GlobalSettings.load(args.vars_file)

    # Apply -e extravars propagation first (lower priority than friendly flags).
    # Shared with proxmox_fleet.cli so the two entry points cannot drift.
    settings = apply_extravar_overrides(settings, extravars)

    # Friendly flags override extravars — applied last so they always win.
    if args.dry_run:
        settings = settings.model_copy(update={"fleet_dry_run": True})
    if args.force_notify:
        settings = settings.model_copy(update={"force_notify": True})
    if args.lxc_verbose:
        settings = settings.model_copy(update={"lxc_verbose": True})
    if args.force_window:
        settings = settings.model_copy(update={"force_window": True})

    return run_locked(settings, lambda: driver.run_fleet(
        settings=settings,
        inventory_path=args.inventory,
        check=args.dry_run,
        extra_vars=extravars,
        limit=_parse_csv_set(args.limit),
        phases=_parse_csv_set(args.phases),
    ))


if __name__ == "__main__":
    raise SystemExit(main())
