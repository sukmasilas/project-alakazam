"""Fixtures for the web-app test suite. Runs against the same disposable
Postgres instance as tests/conftest.py (TEST_DATABASE_URL) — real DB
triggers/unique indexes, not mocked, same reasoning as the Milestone 2
suite (see tests/conftest.py's own docstring).
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from inventory.db import _run_migrations_on_engine, drop_schema, get_engine
from inventory.seed import seed_categories
from tests._db_safety import resolve_test_database_url

TEST_USERNAME = "webapp_test_user"
TEST_PASSWORD = "webapp_test_password_123"
TEST_SECRET_KEY = "test-only-secret-key-never-used-in-production"


@pytest.fixture()
def db_url() -> str:
    return resolve_test_database_url()


@pytest.fixture()
def engine(db_url):
    eng = get_engine(db_url)
    drop_schema(eng)
    _run_migrations_on_engine(eng)
    with eng.begin() as conn:
        seed_categories(conn)
    yield eng
    eng.dispose()


@pytest.fixture()
def app(db_url, engine, monkeypatch):
    # get_settings() requires these env vars even though database_url/
    # secret_key are also passed explicitly to create_app() below — set
    # them so nothing falls back to a real, non-test value.
    monkeypatch.setenv("ALAKAZAM_LOGIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("ALAKAZAM_LOGIN_PASSWORD", TEST_PASSWORD)
    monkeypatch.setenv("ALAKAZAM_SECRET_KEY", TEST_SECRET_KEY)
    monkeypatch.setenv("DATABASE_URL", db_url)

    from webapp.app import create_app

    application = create_app(database_url=db_url, secret_key=TEST_SECRET_KEY)
    yield application
    application.state.engine.dispose()


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def auth_client(client):
    """A TestClient that has already logged in with the real login gate
    (not a bypass) — proves the session cookie itself is what grants
    access, not a test-only shortcut.
    """
    resp = client.post(
        "/login",
        data={"username": TEST_USERNAME, "password": TEST_PASSWORD, "next": "/inventory"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/inventory"
    return client
