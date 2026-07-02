"""Initialize the dashboard user database and create the admin user.

Called by ``install.sh``:

    python -m proxmox_fleet.web.init_db --password "..." --db-path /var/log/fleet-update/.fleet-users.db

Idempotent — if ``admin@fleet.lan`` already exists the existing user is kept
(the password is NOT changed) and the command still exits 0.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional

from proxmox_fleet.web.auth import ADMIN_EMAIL, create_admin_user


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m proxmox_fleet.web.init_db",
        description="Create the dashboard user database and admin account.",
    )
    parser.add_argument("--password", required=True, help="admin password")
    parser.add_argument("--db-path", required=True, help="SQLite database file path")
    args = parser.parse_args(argv)

    if not args.password:
        print("error: password must not be empty", file=sys.stderr)
        return 1

    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        user = asyncio.run(create_admin_user(db_path, args.password))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"admin user ready: {ADMIN_EMAIL} (id {user.id}) in {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
