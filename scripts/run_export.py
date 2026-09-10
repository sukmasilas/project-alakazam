"""Thin CLI wrapper for the Noctrowl export pipeline.

Usage:
    python scripts/run_export.py                # uses DATABASE_URL
    python scripts/run_export.py --database-url postgresql+psycopg2://...

Manual trigger, incremental (CLAUDE.md's "Milestone 4 decisions, confirmed
2026-09-10"): every run exports whatever purchases haven't been exported
yet (``exported_at IS NULL``) and marks them, so re-running this script is
always safe — it never re-sends an already-exported purchase, and if the
upload fails, nothing gets marked so the next run naturally retries the
same rows (see export/runner.py's docstring for the full guarantee).

Requires real Google OAuth credentials to actually reach Drive — see
export/drive_client.py and scripts/authorize_google_drive.py. Until the
user has completed that one-time setup, this script will fail loudly with
a DriveCredentialError explaining exactly what's missing, rather than
silently doing nothing or fabricating a result.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from export.drive_client import DriveCredentialError, DriveClient  # noqa: E402
from export.runner import run_export  # noqa: E402
from inventory.db import get_engine  # noqa: E402


def main() -> int:
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
        return 1

    try:
        drive_client = DriveClient()
    except DriveCredentialError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    engine = get_engine(args.database_url)
    try:
        with engine.begin() as conn:
            result = run_export(conn, drive_client)
    finally:
        engine.dispose()

    if not result.ran_upload:
        print("Nothing to export — every purchase already has exported_at set.")
        return 0

    print(
        f"Exported {len(result.exported_purchase_ids)} purchase(s), "
        f"{result.row_count} row(s), to Drive file {result.filename!r} "
        f"(file id {result.drive_file_id}, folder id {result.drive_folder_id})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
