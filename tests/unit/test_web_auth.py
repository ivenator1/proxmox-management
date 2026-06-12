"""Tests for FastAPI-Users authentication in the web dashboard."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from proxmox_fleet.web.auth import UserManager, get_user_db
from proxmox_fleet.web.models import Base, User


@pytest.fixture
def temp_db_path() -> Path:
    """Create a temporary SQLite database path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test.db"


@pytest.fixture
async def async_engine(temp_db_path: Path):
    """Create async SQLite engine for testing."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def async_session_maker(async_engine):
    """Create async session factory."""
    return sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore


@pytest.fixture
async def user_manager(async_session_maker):
    """Create a UserManager instance for testing."""
    async with async_session_maker() as session:
        user_db = await get_user_db(session)
        manager = UserManager(user_db)
        yield manager


@pytest.mark.asyncio
async def test_create_admin_user(async_session_maker):
    """Test creating an admin user in the database."""
    password = "test-admin-password-123"
    async with async_session_maker() as session:
        user_db = await get_user_db(session)
        manager = UserManager(user_db)

        # Create admin user
        user = await manager.create(
            {
                "email": "admin@localhost",
                "password": password,
                "is_active": True,
                "is_superuser": False,
                "is_verified": True,
            }
        )

        assert user.email == "admin@localhost"
        assert user.is_active is True
        assert user.id is not None

        # Verify password was hashed (not stored in plain text)
        assert user.hashed_password != password
        assert len(user.hashed_password) > len(password)


@pytest.mark.asyncio
async def test_verify_password(async_session_maker):
    """Test password verification."""
    password = "test-password-secure-123"
    async with async_session_maker() as session:
        user_db = await get_user_db(session)
        manager = UserManager(user_db)

        user = await manager.create(
            {
                "email": "admin@localhost",
                "password": password,
                "is_active": True,
                "is_verified": True,
            }
        )

        # Verify correct password
        authenticated_user = await manager.authenticate(
            {"username": "admin@localhost", "password": password}
        )
        assert authenticated_user is not None
        assert authenticated_user.email == "admin@localhost"


@pytest.mark.asyncio
async def test_verify_password_wrong(async_session_maker):
    """Test that wrong password fails."""
    password = "test-password-secure-123"
    wrong_password = "wrong-password"

    async with async_session_maker() as session:
        user_db = await get_user_db(session)
        manager = UserManager(user_db)

        await manager.create(
            {
                "email": "admin@localhost",
                "password": password,
                "is_active": True,
                "is_verified": True,
            }
        )

        # Try to authenticate with wrong password
        authenticated_user = await manager.authenticate(
            {"username": "admin@localhost", "password": wrong_password}
        )
        assert authenticated_user is None


@pytest.mark.asyncio
async def test_duplicate_user(async_session_maker):
    """Test that creating duplicate user fails."""
    async with async_session_maker() as session:
        user_db = await get_user_db(session)
        manager = UserManager(user_db)

        # Create first admin user
        await manager.create(
            {
                "email": "admin@localhost",
                "password": "password-123",
                "is_active": True,
                "is_verified": True,
            }
        )

        # Try to create duplicate
        with pytest.raises(Exception):  # FastAPI-Users raises ValueError for duplicate
            await manager.create(
                {
                    "email": "admin@localhost",
                    "password": "password-456",
                    "is_active": True,
                    "is_verified": True,
                }
            )


@pytest.mark.asyncio
async def test_user_get_by_email(async_session_maker):
    """Test retrieving user by email."""
    email = "testuser@example.com"

    async with async_session_maker() as session:
        user_db = await get_user_db(session)
        manager = UserManager(user_db)

        created_user = await manager.create(
            {
                "email": email,
                "password": "password-123",
                "is_active": True,
                "is_verified": True,
            }
        )

        # Retrieve by email
        retrieved_user = await manager.get_by_email(email)
        assert retrieved_user is not None
        assert retrieved_user.email == email
        assert retrieved_user.id == created_user.id


@pytest.mark.asyncio
async def test_user_not_found(async_session_maker):
    """Test retrieving non-existent user."""
    async with async_session_maker() as session:
        user_db = await get_user_db(session)
        manager = UserManager(user_db)

        # Try to get non-existent user
        with pytest.raises(Exception):  # FastAPI-Users raises for not found
            await manager.get_by_email("nonexistent@example.com")


class TestDashboardAuth:
    """Integration tests for dashboard authentication."""

    @pytest.fixture
    def app_with_auth(self, async_session_maker):
        """Create a test FastAPI app with authentication."""
        from fastapi import Depends
        from proxmox_fleet.web.auth import current_active_user, fastapi_users

        app = FastAPI()

        # Override dependency
        async def get_session():  # type: ignore
            async with async_session_maker() as session:
                yield session

        async def get_user_db_test(session=Depends(get_session)):  # type: ignore
            yield await get_user_db(session)

        from proxmox_fleet.web import auth
        app.dependency_overrides[auth.get_user_db] = get_user_db_test

        # Mount auth routes
        app.include_router(
            fastapi_users.get_auth_router("session"),
            prefix="/auth",
            tags=["auth"],
        )

        # Protected route
        @app.get("/protected")
        async def protected_route(_: User = Depends(current_active_user)):  # type: ignore
            return {"message": "success", "email": _.email}

        return app

    def test_login_form_accessible(self, app_with_auth):
        """Test that login form is accessible without authentication."""
        client = TestClient(app_with_auth)
        # Session login endpoint should be accessible
        response = client.get("/auth/login")
        assert response.status_code == 200

    def test_protected_route_requires_auth(self, app_with_auth):
        """Test that protected routes require authentication."""
        client = TestClient(app_with_auth)
        response = client.get("/protected")
        # Should get 403 without auth
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_login_and_access_protected(self, app_with_auth, async_session_maker):
        """Test login flow and protected route access."""
        # Create user in DB
        async with async_session_maker() as session:
            user_db = await get_user_db(session)
            manager = UserManager(user_db)
            await manager.create(
                {
                    "email": "testuser@localhost",
                    "password": "test-password-123",
                    "is_active": True,
                    "is_verified": True,
                }
            )

        client = TestClient(app_with_auth)

        # Login
        login_response = client.post(
            "/auth/login",
            data={"username": "testuser@localhost", "password": "test-password-123"},
        )
        # Should redirect to the original page or return success
        assert login_response.status_code in [200, 302]

        # After login, protected route should be accessible
        response = client.get("/protected")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_wrong_password_login_fails(self, app_with_auth, async_session_maker):
        """Test that login with wrong password fails."""
        # Create user in DB
        async with async_session_maker() as session:
            user_db = await get_user_db(session)
            manager = UserManager(user_db)
            await manager.create(
                {
                    "email": "testuser@localhost",
                    "password": "correct-password",
                    "is_active": True,
                    "is_verified": True,
                }
            )

        client = TestClient(app_with_auth)

        # Try to login with wrong password
        response = client.post(
            "/auth/login",
            data={"username": "testuser@localhost", "password": "wrong-password"},
        )
        # Should fail (400 or 401)
        assert response.status_code in [400, 401, 422]

        # Protected route should still require auth
        response = client.get("/protected")
        assert response.status_code == 403


class TestInitDb:
    """Tests for the init_db module."""

    @pytest.mark.asyncio
    async def test_init_db_creates_user(self, temp_db_path):
        """Test that init_db creates admin user."""
        from proxmox_fleet.web.init_db import create_admin_user

        password = "test-admin-password"
        db_url = f"sqlite+aiosqlite:///{temp_db_path}"

        # Create admin user
        await create_admin_user(db_url, password)

        # Verify user was created
        engine = create_async_engine(db_url)
        async_session_maker = sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )  # type: ignore

        async with async_session_maker() as session:
            user_db = await get_user_db(session)
            manager = UserManager(user_db)
            user = await manager.get_by_email("admin@localhost")
            assert user is not None
            assert user.is_active is True

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_init_db_idempotent(self, temp_db_path):
        """Test that init_db is idempotent (doesn't fail on second run)."""
        from proxmox_fleet.web.init_db import create_admin_user

        password = "test-admin-password"
        db_url = f"sqlite+aiosqlite:///{temp_db_path}"

        # Create admin user first time
        await create_admin_user(db_url, password)

        # Create again - should not raise
        await create_admin_user(db_url, password)  # Same or different password is OK


@pytest.mark.asyncio
async def test_user_model_fields():
    """Test that User model has expected fields."""
    # User should have inherited fields from SQLAlchemyBaseUserTable
    assert hasattr(User, "id")
    assert hasattr(User, "email")
    assert hasattr(User, "hashed_password")
    assert hasattr(User, "is_active")
    assert hasattr(User, "is_verified")
