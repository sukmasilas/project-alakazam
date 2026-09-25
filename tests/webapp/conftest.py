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


@pytest.fixture()
def prefixed_client(app):
    # Exercises ASGI root_path support (see CLAUDE.md's "Real production
    # deployment shape" entry: nginx proxies dotworks.net/alakazam/* here,
    # prefix-stripped, with the app run as `uvicorn ... --root-path
    # /alakazam`).
    #
    # IMPORTANT / non-obvious (verified directly against real uvicorn's
    # httptools_impl.py before writing this): real uvicorn sets
    # scope["path"] = root_path + <the request path actually received on
    # the wire> — i.e. scope["path"] always INCLUDES the root_path prefix,
    # and Starlette's routing (get_route_path in starlette/_utils.py)
    # strips that prefix back off before matching routes. Starlette's own
    # TestClient, by contrast, does NOT perform that concatenation — it
    # sets scope["path"] to *exactly* whatever string is passed to
    # .get()/.post(), with no root_path prefix added. So the faithful way
    # to unit-test root_path handling through TestClient is to pass the
    # ALREADY-PREFIXED path yourself (e.g. `client.get("/alakazam/inventory")`
    # with this fixture) — that's what reproduces the scope real uvicorn
    # would actually construct.
    #
    # This is the OPPOSITE convention from testing against a real running
    # uvicorn process (see the manual verification in the task report):
    # there, you curl the UNPREFIXED path, because real uvicorn re-adds
    # root_path itself and double-prefixing would 404. Confirmed empirically
    # (not just reasoned about) — plain `client.get(unprefixed_path)`
    # against this fixture 404s on the StaticFiles Mount, because the Mount
    # only strips root_path from scope["path"] when scope["path"] actually
    # starts with it, which is only true for a real uvicorn-constructed
    # scope. Top-level (non-Mount) routes happened to still match via a
    # defensive fallback in get_route_path, which is why this distinction
    # is easy to miss for simple pages but breaks on nested Mounts (e.g.
    # the /static StaticFiles mount) — always use the prefixed-path
    # convention with this fixture.
    return TestClient(app, root_path="/alakazam")
