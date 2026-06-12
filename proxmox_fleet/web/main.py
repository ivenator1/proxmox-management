"""``fleet-dashboard`` console entrypoint — uvicorn server for the web app.

Run from the project root (the run trigger launches ``python -m
proxmox_fleet.cli``, which needs CWD = project root for ansible-runner).
Bind address/port come from ``vars.yml`` (``dashboard_host`` /
``dashboard_port``) with CLI overrides.
"""

from __future__ import annotations

import argparse
from typing import List, Optional

from proxmox_fleet.models.settings import GlobalSettings


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fleet-dashboard",
        description="Fleet web dashboard: pending updates, run history, run trigger.",
    )
    parser.add_argument("--vars-file", default="vars.yml",
                        help="path to vars.yml used to load GlobalSettings.")
    parser.add_argument("--host", default=None,
                        help="bind address (default: dashboard_host from vars.yml).")
    parser.add_argument("--port", type=int, default=None,
                        help="bind port (default: dashboard_port from vars.yml).")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - defensive
        raise SystemExit(
            "uvicorn is not installed — the dashboard is an optional extra: "
            "pip install -e '.[web]'"
        )

    from proxmox_fleet.web.app import create_app

    settings = GlobalSettings.load(args.vars_file)
    if not settings.dashboard_token:
        print("WARNING: dashboard_token is empty — the run trigger is unauthenticated")

    uvicorn.run(
        create_app(settings, vars_path=args.vars_file),
        host=args.host if args.host is not None else settings.dashboard_host,
        port=args.port if args.port is not None else settings.dashboard_port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
