"""Apply every .sql file in migrations/, in filename order, exactly once.

Usage:
    python scripts/run_migrations.py                # uses DATABASE_URL
    python scripts/run_migrations.py --database-url postgresql+psycopg2://...

Thin CLI wrapper — the real logic lives in inventory/db.py (run_migrations),
so tests and any future app code can call the same function directly
without shelling out to this script.

Never hardcodes a connection string — reads DATABASE_URL from the
environment (optionally via a local .env, loaded through python-dotenv) or
an explicit --database-url argument.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inventory.db import run_migrations  # noqa: E402


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres connection string. Defaults to the DATABASE_URL env var.",
    )
    args = parser.parse_args()

    if not args.database_url:
        print(
            "No database URL provided. Set DATABASE_URL in the environment "
            "(or a .env file) or pass --database-url explicitly.",
            file=sys.stderr,
        )
        sys.exit(1)

    applied = run_migrations(args.database_url)
    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Database already up to date — no migrations applied.")


if __name__ == "__main__":
    main()
