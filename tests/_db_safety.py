"""Shared safety guard for resolving the disposable test database URL.

Same pattern as Project-Noctrowl's own tests/_db_safety.py (sibling
project) — kept as an independent copy rather than a shared import, since
these are two separate codebases/repos.

Two independent guards, both required:

1. ``TEST_DATABASE_URL`` must be set explicitly. There is deliberately NO
   fallback to ``DATABASE_URL`` — that variable is allowed to point at a
   real, persistent database and must never be touched by this test
   suite's schema drop/recreate.
2. The resolved database name must look disposable (contains "test",
   case-insensitive) — a misconfigured environment variable pointed at a
   real database is refused even if it arrives via ``TEST_DATABASE_URL``.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when the resolved test database doesn't look disposable."""


def resolve_test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL is not set. The test suite drops and recreates "
            "its schema against this database on every run, so it must NEVER "
            "fall back to DATABASE_URL (which may point at a real, persistent "
            "database). Set TEST_DATABASE_URL explicitly to a disposable "
            "Postgres instance, e.g.:\n\n"
            "    docker run -d --name alakazam-test-pg \\\n"
            "        -e POSTGRES_USER=alakazam -e POSTGRES_PASSWORD=testpass \\\n"
            "        -e POSTGRES_DB=alakazam_test -p 55433:5432 postgres:16-alpine\n\n"
            "    export TEST_DATABASE_URL=postgresql+psycopg2://alakazam:testpass@localhost:55433/alakazam_test\n"
            "    pytest\n"
        )

    db_name = (urlsplit(url).path or "").lstrip("/")
    if "test" not in db_name.lower():
        raise UnsafeTestDatabaseError(
            f"Refusing to run the test suite against database {db_name!r} "
            "(resolved from TEST_DATABASE_URL). The database name must "
            "contain 'test' to proceed."
        )

    return url
