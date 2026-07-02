"""Initialize the dashboard user database and create the admin user.

Called by ``install.sh``:

    FLEET_ADMIN_PASSWORD="..." python -m proxmox_fleet.web.init_db

The password comes from ``--password`` or (preferred for scripts — argv is
visible in ``ps``) the ``FLEET_ADMIN_PASSWORD`` environment variable. The DB
location defaults to ``auth.user_db_path(fleet_history_dir)`` with
``fleet_history_dir`` read from ``--vars-file`` — the same resolution the
dashboard itself uses — so a custom history dir never strands the admin
account in the wrong directory; ``--db-path`` overrides it (tests).

Idempotent — if ``admin@fleet.lan`` already exists the existing user is kept
(the password is NOT changed) and the command still exits 0.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import List, Optional

from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.web.auth import ADMIN_EMAIL, create_admin_user, user_db_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m proxmox_fleet.web.init_db",
        description="Create the dashboard user database and admin account.",
    )
    parser.add_argument("--password", default=None,
                        help="admin password (default: $FLEET_ADMIN_PASSWORD)")
    parser.add_argument("--db-path", default=None,
                        help="SQLite database file path "
                             "(default: <fleet_history_dir>/.fleet-users.db)")
    parser.add_argument("--vars-file", default="vars.yml",
                        help="vars.yml used to resolve fleet_history_dir "
                             "when --db-path is not given")
    args = parser.parse_args(argv)

    password = args.password or os.environ.get("FLEET_ADMIN_PASSWORD") or ""
    if not password:
        print("error: password must not be empty "
              "(pass --password or set FLEET_ADMIN_PASSWORD)", file=sys.stderr)
        return 1

    if args.db_path is not None:
        db_path = Path(args.db_path)
    else:
        settings = GlobalSettings.load(args.vars_file)
        db_path = user_db_path(settings.fleet_history_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        user = asyncio.run(create_admin_user(db_path, password))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"admin user ready: {ADMIN_EMAIL} (id {user.id}) in {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
