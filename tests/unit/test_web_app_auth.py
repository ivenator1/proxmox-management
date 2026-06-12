"""Integration tests for the dashboard with authentication."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from proxmox_fleet.models.settings import GlobalSettings
from proxmox_fleet.web.app import create_app
from proxmox_fleet.web.auth import UserManager, get_user_db
from proxmox_fleet.web.models import Base


@pytest.fixture
def temp_db_path() -> Path:
    """Create a temporary SQLite database path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "test.db"


@pytest.fixture
async def async_engine_for_app(temp_db_path: Path):
    """Create async SQLite engine for app testing."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def admin_user_in_db(async_engine_for_app):
    """Create an admin user in the test database."""
    async_session_maker = sessionmaker(
        async_engine_for_app, class_=AsyncSession, expire_on_commit=False
    )  # type: ignore

    async with async_session_maker() as session:
        user_db = await get_user_db(session)
        manager = UserManager(user_db)
        user = await manager.create(
            {
                "email": "admin@localhost",
                "password": "test-password-123",
                "is_active": True,
                "is_verified": True,
            }
        )
        yield user


@pytest.fixture
def app_with_test_db(temp_db_path, admin_user_in_db):
    """Create a test app instance with test database."""
    # Create mock settings
    settings = MagicMock(spec=GlobalSettings)
    settings.fleet_history_dir = str(temp_db_path.parent)
    settings.dashboard_host = "127.0.0.1"
    settings.dashboard_port = 8421

    # Create app with mock run manager
    run_manager = MagicMock()
    run_manager.active_run.return_value = None
    run_manager.list_runs.return_value = []

    app = create_app(
        settings=settings,
        run_manager=run_manager,
        vars_path="vars.yml.example",
        inventory_path="hosts.ini.example",
    )

    return app


class TestDashboardAuthIntegration:
    """Integration tests for dashboard with authentication."""

    def test_login_page_accessible(self, app_with_test_db):
        """Test that login page is accessible without auth."""
        client = TestClient(app_with_test_db)
        response = client.get("/auth/login")
        assert response.status_code == 200
        # Should serve HTML
        assert "text/html" in response.headers.get("content-type", "")

    def test_protected_routes_require_auth(self, app_with_test_db):
        """Test that protected routes return 403 without auth."""
        client = TestClient(app_with_test_db)

        protected_routes = ["/", "/pending", "/history", "/trigger", "/inventory", "/settings"]

        for route in protected_routes:
            response = client.get(route)
            # Without auth, should get 403 or redirect to login
            assert response.status_code in [403, 307, 308], f"Route {route} returned {response.status_code}"

    def test_logout_route_exists(self, app_with_test_db):
        """Test that logout route exists."""
        client = TestClient(app_with_test_db)
        response = client.post("/auth/logout")
        # Should succeed even without login (just clears session)
        assert response.status_code in [200, 302, 307, 308]

    def test_auth_router_mounted(self, app_with_test_db):
        """Test that auth router is properly mounted."""
        # Check that auth routes exist in the app
        routes = [route.path for route in app_with_test_db.routes]
        assert "/auth/login" in routes
        assert "/auth/logout" in routes

    def test_static_files_accessible_without_auth(self, app_with_test_db):
        """Test that static files don't require authentication."""
        client = TestClient(app_with_test_db)
        # Static files should be accessible (they're served by StaticFiles middleware)
        # The middleware should handle them before auth checks
        response = client.get("/static/dashboard.css")
        # Should get 404 (file doesn't exist in test) or 200 (file exists)
        # But NOT 403 (forbidden)
        assert response.status_code != 403


class TestDashboardLoginFlow:
    """Test the complete login flow."""

    @pytest.mark.asyncio
    async def test_login_creates_session(self, app_with_test_db, async_engine_for_app):
        """Test that successful login creates a session cookie."""
        client = TestClient(app_with_test_db)

        # Login with valid credentials
        response = client.post(
            "/auth/login",
            data={"username": "admin@localhost", "password": "test-password-123"},
        )

        # Should be successful (200, 302, etc. depending on implementation)
        assert response.status_code in [200, 302, 307]

        # Check that a session cookie was set
        cookies = client.cookies
        # FastAPI-Users sets a "session" or "fastapiusersauth" cookie
        assert len(cookies) > 0

    def test_login_with_invalid_credentials(self, app_with_test_db):
        """Test that login with wrong password fails."""
        client = TestClient(app_with_test_db)

        response = client.post(
            "/auth/login",
            data={"username": "admin@localhost", "password": "wrong-password"},
        )

        # Should fail (4xx status)
        assert response.status_code >= 400

    def test_login_with_nonexistent_user(self, app_with_test_db):
        """Test that login with non-existent user fails."""
        client = TestClient(app_with_test_db)

        response = client.post(
            "/auth/login",
            data={"username": "nonexistent@localhost", "password": "password"},
        )

        # Should fail (4xx status)
        assert response.status_code >= 400
