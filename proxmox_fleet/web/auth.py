"""FastAPI-Users authentication setup: database, user manager, auth backends."""
from __future__ import annotations

from typing import Optional

from fastapi import Depends
from fastapi_users import BaseUserManager, FastAPIUsers
from fastapi_users.authentication import AuthenticationBackend, SessionStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from proxmox_fleet.web.models import User


class UserManager(BaseUserManager[User, int]):
    """User manager for FastAPI-Users."""

    reset_password_token_secret = "CHANGE-THIS-IN-PRODUCTION"  # nosec - override per-deploy if needed
    verification_token_secret = "CHANGE-THIS-IN-PRODUCTION"

    async def on_after_register(
        self, user: User, request=None
    ) -> None:  # type: ignore
        """Called after user registration."""
        pass

    async def on_after_forgot_password(
        self, user: User, token: str, request=None
    ) -> None:  # type: ignore
        """Called after password reset request."""
        pass

    async def on_after_request_verify(
        self, user: User, token: str, request=None
    ) -> None:  # type: ignore
        """Called after email verification request."""
        pass


async def get_user_db(session: AsyncSession) -> SQLAlchemyUserDatabase[User, int]:  # type: ignore
    """Dependency to get the user database."""
    yield SQLAlchemyUserDatabase(session, User)


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase[User, int] = Depends(get_user_db),
) -> UserManager:  # type: ignore
    """Dependency to get the user manager."""
    yield UserManager(user_db)


# Session-based auth backend (HTTP-only cookie)
auth_backend = AuthenticationBackend(
    name="session",
    transport=SessionStrategy(
        lifetime_seconds=None,  # Session lifetime in seconds (None = until browser closes)
    ),
)

# FastAPI-Users instance for route mounting and dependencies
fastapi_users: FastAPIUsers[User, int] = FastAPIUsers[User, int](  # type: ignore
    get_user_manager, [auth_backend]
)

# Current user dependency for route protection
current_active_user = fastapi_users.current_user(active=True)
