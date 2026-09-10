"""Test fixtures.

Tests run against a real PostgreSQL database — deliberate, since the
reconciliation invariant and the SKU/serial uniqueness constraints are
implemented as real Postgres triggers / unique indexes (see
migrations/001_initial_schema.sql), and constraint-timing or
Decimal-vs-float bugs don't reliably surface against a different engine.

Point TEST_DATABASE_URL at a disposable Postgres instance before running
the suite, e.g.:

    docker run -d --name alakazam-test-pg \\
        -e POSTGRES_USER=alakazam -e POSTGRES_PASSWORD=testpass \\
        -e POSTGRES_DB=alakazam_test -p 55433:5432 postgres:16-alpine

    export TEST_DATABASE_URL=postgresql+psycopg2://alakazam:testpass@localhost:55433/alakazam_test
    pytest

Never point this at a real/persistent database — the schema is dropped and
recreated for every test run. See tests/_db_safety.py for the guards.
"""
from __future__ import annotations

import pytest

from inventory.db import _run_migrations_on_engine, drop_schema, get_engine
from inventory.seed import seed_categories
from tests._db_safety import resolve_test_database_url


@pytest.fixture()
def engine():
    eng = get_engine(resolve_test_database_url())
    drop_schema(eng)
    _run_migrations_on_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def conn(engine):
    """A connection with the 5 confirmed categories already seeded. Wraps
    the test in one open transaction; teardown rolls it back (the next
    test gets a fresh schema from the ``engine`` fixture regardless).
    """
    with engine.connect() as connection:
        seed_categories(connection)
        yield connection
        connection.rollback()
