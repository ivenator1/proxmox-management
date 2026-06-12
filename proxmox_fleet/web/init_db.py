"""Initialize the user database and create the admin user.

Usage:
    python -m proxmox_fleet.web.init_db --password "ADMIN_PASSWORD" --db-path "/var/log/fleet-update/.fleet-users.db"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from proxmox_fleet.web.auth import UserManager, get_user_db
from proxmox_fleet.web.models import Base, User


async def create_admin_user(
    db_url: str,
    password: str,
) -> None:
    """Create the SQLite database and admin user."""
    # Create async engine
    engine = create_async_engine(db_url, echo=False)

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Create session factory
    async_session_maker = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False  # type: ignore
    )

    # Create user manager and add admin user
    async with async_session_maker() as session:
        user_db = await get_user_db(session)
        user_manager = UserManager(user_db)

        # Check if admin user already exists
        try:
            existing = await user_manager.get_by_email("admin@localhost")
            print(f"Admin user already exists (ID: {existing.id})")
            return
        except Exception:
            pass  # User doesn't exist, create it

        # Create admin user
        user = await user_manager.create(
            {
                "email": "admin@localhost",
                "password": password,
                "is_active": True,
                "is_superuser": False,
                "is_verified": True,
            },
        )
        print(f"Created admin user (ID: {user.id}, email: admin@localhost)")

    await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="python -m proxmox_fleet.web.init_db",
        description="Initialize the user database and create the admin user.",
    )
    parser.add_argument(
        "--password", required=True, help="Admin user password"
    )
    parser.add_argument(
        "--db-path",
        required=True,
        help="Path to the SQLite database file",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # SQLite URL (use sqlite+aiosqlite for async)
    db_url = f"sqlite+aiosqlite:///{db_path}"

    try:
        asyncio.run(create_admin_user(db_url, args.password))
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
