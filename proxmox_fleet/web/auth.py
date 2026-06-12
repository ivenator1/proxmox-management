"""FastAPI-Users authentication wiring for the dashboard.

Standard fastapi-users cookie setup (https://fastapi-users.github.io/): a
``CookieTransport`` (HTTP-only cookie) carrying a ``JWTStrategy`` token.
The signing secret is generated once and persisted next to the user DB
(``.fleet-auth-secret``) so logins survive dashboard restarts.

The SQLite engine depends on runtime settings (``fleet_history_dir``), so the
module is configured by :func:`configure` (called from ``create_app``) and the
FastAPI dependencies read the configured session factory lazily.
"""
from __future__ import annotations

import secrets as _secrets
from pathlib import Path
from typing import Any, AsyncGenerator, Optional, Union

from fastapi import Depends
from fastapi_users import BaseUserManager, FastAPIUsers, IntegerIDMixin, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from proxmox_fleet.web.models import Base, User

COOKIE_NAME = "fleet_auth"
SESSION_LIFETIME_SECONDS = 7 * 24 * 3600  # 7 days
ADMIN_EMAIL = "admin@fleet.lan"

# Set by configure(); dependencies below read them lazily.
_engine: Optional[AsyncEngine] = None
_session_maker: Optional[async_sessionmaker[AsyncSession]] = None
_secret: str = ""


class UserCreate(schemas.BaseUserCreate):
    """Standard fastapi-users creation schema (email + password)."""


def _load_or_create_secret(path: Path) -> str:
    """Persisted JWT signing secret — generated once so sessions survive
    restarts; 0600 because possession of it mints valid sessions."""
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = _secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return value


def configure(db_path: Union[str, Path]) -> None:
    """Point the auth layer at the SQLite user DB (idempotent)."""
    global _engine, _session_maker, _secret
    db_path = Path(db_path)
    _secret = _load_or_create_secret(db_path.parent / ".fleet-auth-secret")
    if _engine is None:
        _engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
        _session_maker = async_sessionmaker(_engine, expire_on_commit=False)


async def create_db_and_tables() -> None:
    """Create the user table if missing (called on app startup)."""
    assert _engine is not None, "auth.configure() must be called first"
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    assert _session_maker is not None, "auth.configure() must be called first"
    async with _session_maker() as session:
        yield session


async def get_user_db(
    session: AsyncSession = Depends(get_async_session),
) -> AsyncGenerator[SQLAlchemyUserDatabase[User, int], None]:
    yield SQLAlchemyUserDatabase(session, User)


class UserManager(IntegerIDMixin, BaseUserManager[User, int]):
    """Single-admin user manager; secrets are only used by the (unmounted)
    reset/verify flows but must be non-empty."""

    @property
    def reset_password_token_secret(self) -> str:  # type: ignore[override]
        return _secret or "unconfigured"

    @property
    def verification_token_secret(self) -> str:  # type: ignore[override]
        return _secret or "unconfigured"


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase[User, int] = Depends(get_user_db),
) -> AsyncGenerator[UserManager, None]:
    yield UserManager(user_db)


def _get_jwt_strategy() -> JWTStrategy[User, int]:
    assert _secret, "auth.configure() must be called first"
    return JWTStrategy(secret=_secret, lifetime_seconds=SESSION_LIFETIME_SECONDS)


# cookie_secure=False: the dashboard is plain-HTTP on a trusted LAN by design;
# a Secure cookie would never be sent and every login would silently fail.
_cookie_transport = CookieTransport(
    cookie_name=COOKIE_NAME,
    cookie_max_age=SESSION_LIFETIME_SECONDS,
    cookie_secure=False,
    cookie_httponly=True,
    cookie_samesite="lax",
)

auth_backend: AuthenticationBackend[User, int] = AuthenticationBackend(
    name="cookie",
    transport=_cookie_transport,
    get_strategy=_get_jwt_strategy,
)

fastapi_users: FastAPIUsers[User, int] = FastAPIUsers(get_user_manager, [auth_backend])

# Route-protection dependency: raises 401 when the cookie is missing/invalid.
current_active_user = fastapi_users.current_user(active=True)


async def create_admin_user(db_path: Union[str, Path], password: str) -> Any:
    """Create the single admin user (idempotent — returns the existing user
    if already present). Used by ``init_db`` and tests."""
    from fastapi_users.exceptions import UserAlreadyExists, UserNotExists

    configure(db_path)
    await create_db_and_tables()
    assert _session_maker is not None
    async with _session_maker() as session:
        user_db = SQLAlchemyUserDatabase(session, User)
        manager = UserManager(user_db)
        try:
            return await manager.get_by_email(ADMIN_EMAIL)
        except UserNotExists:
            pass
        try:
            return await manager.create(
                UserCreate(email=ADMIN_EMAIL, password=password, is_verified=True),
                safe=False,
            )
        except UserAlreadyExists:  # pragma: no cover - race with another init
            return await manager.get_by_email(ADMIN_EMAIL)


def reset_for_tests() -> None:
    """Drop module state so tests can re-configure against a fresh temp DB."""
    global _engine, _session_maker, _secret
    _engine = None
    _session_maker = None
    _secret = ""
