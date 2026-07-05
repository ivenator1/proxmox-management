"""Tests for the dashboard login (fastapi-users cookie auth) and init_db.

Sync tests only: async setup goes through ``asyncio.run`` and HTTP through
``TestClient`` — no pytest-asyncio dependency. Skipped wholesale when the
``[web]`` extra is not installed.
"""
import asyncio

import pytest

pytest.importorskip("fastapi_users")

from fastapi.testclient import TestClient  # noqa: E402

from proxmox_fleet.models.settings import GlobalSettings  # noqa: E402
from proxmox_fleet.web import auth  # noqa: E402
from proxmox_fleet.web.app import create_app  # noqa: E402
from proxmox_fleet.web.init_db import main as init_db_main  # noqa: E402

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def fresh_auth_state():
    """Each test gets its own engine/secret (auth holds module-level state)."""
    auth.reset_for_tests()
    yield
    auth.reset_for_tests()


@pytest.fixture
def history_dir(tmp_path):
    d = tmp_path / "history"
    d.mkdir()
    return d


def _create_admin(history_dir, password=PASSWORD):
    return asyncio.run(auth.create_admin_user(history_dir / ".fleet-users.db", password))


def _client(history_dir) -> TestClient:
    settings = GlobalSettings(fleet_history_dir=str(history_dir))
    return TestClient(create_app(settings))


def _login(client: TestClient, password=PASSWORD):
    return client.post("/auth/login",
                       data={"username": auth.ADMIN_EMAIL, "password": password})


# --- user creation / init_db ------------------------------------------------ #

def test_create_admin_hashes_password(history_dir):
    user = _create_admin(history_dir)
    assert user.email == auth.ADMIN_EMAIL
    assert user.is_active
    assert PASSWORD not in user.hashed_password


def test_create_admin_idempotent_keeps_password(history_dir):
    first = _create_admin(history_dir)
    second = _create_admin(history_dir, password="something else")
    assert second.id == first.id
    assert second.hashed_password == first.hashed_password


def test_init_db_cli_creates_db(history_dir):
    db = history_dir / "nested" / ".fleet-users.db"
    rc = init_db_main(["--password", PASSWORD, "--db-path", str(db)])
    assert rc == 0
    assert db.is_file()


def test_init_db_cli_rejects_empty_password(history_dir, monkeypatch):
    monkeypatch.delenv("FLEET_ADMIN_PASSWORD", raising=False)
    rc = init_db_main(["--password", "", "--db-path",
                       str(history_dir / ".fleet-users.db")])
    assert rc == 1


def test_init_db_cli_password_from_env(history_dir, monkeypatch):
    """install.sh passes the password via the environment (argv shows in ps)."""
    monkeypatch.setenv("FLEET_ADMIN_PASSWORD", PASSWORD)
    db = history_dir / ".fleet-users.db"
    rc = init_db_main(["--db-path", str(db)])
    assert rc == 0
    assert db.is_file()


def test_init_db_cli_resolves_db_path_from_vars(history_dir, tmp_path):
    """Without --db-path the DB lands at user_db_path(fleet_history_dir) —
    the same resolution create_app uses, so a custom history dir can never
    strand the admin account in the wrong directory."""
    vars_file = tmp_path / "vars.yml"
    vars_file.write_text(f"fleet_history_dir: {history_dir}\n", encoding="utf-8")
    rc = init_db_main(["--password", PASSWORD, "--vars-file", str(vars_file)])
    assert rc == 0
    assert auth.user_db_path(history_dir).is_file()


def test_auth_secret_persisted(history_dir):
    _create_admin(history_dir)
    secret_file = history_dir / ".fleet-auth-secret"
    assert secret_file.is_file()
    first = secret_file.read_text()
    auth.reset_for_tests()
    auth.configure(history_dir / ".fleet-users.db")
    assert secret_file.read_text() == first  # reused, not regenerated


def test_credential_files_owner_only(history_dir):
    """Both the JWT secret and the user DB (password hash) must be 0600."""
    _create_admin(history_dir)
    for name in (".fleet-auth-secret", ".fleet-users.db"):
        mode = (history_dir / name).stat().st_mode & 0o777
        assert mode == 0o600, f"{name} is {oct(mode)}"


# --- login flow --------------------------------------------------------------#

def test_login_sets_cookie_and_grants_access(history_dir):
    _create_admin(history_dir)
    client = _client(history_dir)

    assert client.get("/").status_code == 401          # locked before login
    resp = _login(client)
    assert resp.status_code == 204
    assert auth.COOKIE_NAME in client.cookies

    for page in ("/", "/pending", "/history", "/trigger", "/inventory"):
        assert client.get(page).status_code == 200, page


def test_wrong_password_rejected(history_dir):
    _create_admin(history_dir)
    client = _client(history_dir)
    assert _login(client, password="wrong").status_code == 400
    assert client.get("/").status_code == 401


def test_unknown_user_rejected(history_dir):
    _create_admin(history_dir)
    client = _client(history_dir)
    resp = client.post("/auth/login",
                       data={"username": "evil@fleet.lan", "password": PASSWORD})
    assert resp.status_code == 400


def test_logout_clears_session(history_dir):
    _create_admin(history_dir)
    client = _client(history_dir)
    _login(client)
    assert client.get("/").status_code == 200
    client.post("/auth/logout")
    assert client.get("/").status_code == 401


def test_every_page_locked_without_login(history_dir):
    _create_admin(history_dir)
    client = _client(history_dir)
    pages = ("/", "/pending", "/history", "/history/latest", "/hosts/somehost",
             "/trigger", "/inventory", "/settings",
             # the run console/log/stream leak fleet output if left open —
             # 401 must win over 404 even for unknown run ids
             "/runs/20260101T000000000000Z", "/runs/20260101T000000000000Z/log",
             "/runs/20260101T000000000000Z/stream")
    for page in pages:
        assert client.get(page).status_code == 401, page
    posts = ("/runs", "/inventory/add", "/inventory/remove", "/inventory/create",
             "/settings", "/settings/raw", "/ssh/generate", "/ssh/push", "/ssh/test")
    for page in posts:
        assert client.post(page, data={}).status_code == 401, page


def test_login_page_and_static_open_without_session(history_dir):
    _create_admin(history_dir)
    client = _client(history_dir)
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text
    # static assets must load for the login page itself
    assert client.get("/static/dashboard.css").status_code == 200


def test_browser_request_redirects_to_login(history_dir):
    _create_admin(history_dir)
    client = _client(history_dir)
    resp = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_api_request_gets_plain_401(history_dir):
    _create_admin(history_dir)
    client = _client(history_dir)
    resp = client.get("/", headers={"accept": "application/json"})
    assert resp.status_code == 401


def test_forged_cookie_rejected(history_dir):
    _create_admin(history_dir)
    client = _client(history_dir)
    client.cookies.set(auth.COOKIE_NAME, "forged.jwt.value")
    assert client.get("/").status_code == 401
