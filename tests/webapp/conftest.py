"""Fixtures for the web-app test suite. Runs against the same disposable
Postgres instance as tests/conftest.py (TEST_DATABASE_URL) — real DB
triggers/unique indexes, not mocked, same reasoning as the Milestone 2
suite (see tests/conftest.py's own docstring).

As of 2026-09-22, Alakazam has no login gate of its own (see
webapp/app.py's module docstring) — there is no authenticated-vs-
unauthenticated client distinction to test anymore, so there is only one
``client`` fixture.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from inventory.db import _run_migrations_on_engine, drop_schema, get_engine
from inventory.seed import seed_categories
from tests._db_safety import resolve_test_database_url


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
    # get_settings() requires DATABASE_URL even though it's also passed
    # explicitly to create_app() below — set it so nothing falls back to a
    # real, non-test value.
    monkeypatch.setenv("DATABASE_URL", db_url)

    from webapp.app import create_app

    application = create_app(database_url=db_url)
    yield application
    application.state.engine.dispose()


@pytest.fixture()
def client(app):
    return TestClient(app)
