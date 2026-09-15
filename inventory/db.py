"""Engine creation + migration application.

Never hardcodes a connection string — every entry point here takes an
explicit ``database_url`` (or reads it from an env var one layer up, e.g.
scripts/run_migrations.py or tests/conftest.py).
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_ENSURE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Every real object migration 001 (and any future migration) creates,
# newest-dependent-first, for drop_schema()'s use in tests only. Kept as an
# explicit list (not introspection) so drop_schema() can never accidentally
# reach a database it shouldn't and drop something unexpected.
_ALL_OBJECTS_NEWEST_FIRST = [
    # Migration 006 (Milestone 8 — consignment tracking). The items.consignor_id
    # column and serial_units.purchase_line_item_id becoming nullable need no
    # separate entry — those two tables are already listed further down
    # (migration 001) and DROP TABLE ... CASCADE removes everything about them.
    ("TABLE", "consignor_reimbursements"),
    ("TRIGGER", "trg_check_serial_unit_consignment_consistency ON serial_units"),
    ("FUNCTION", "check_serial_unit_consignment_consistency()"),
    ("TRIGGER", "trg_check_item_consignor_requires_serialized ON items"),
    ("FUNCTION", "check_item_consignor_requires_serialized()"),
    ("TABLE", "consignors"),
    # Migration 005 (Milestone 7 — pre-order/dropship sales). The
    # `preorder_sale_id` column/partial unique index added to
    # fungible_depletions needs no separate entry — that whole table is
    # already dropped (CASCADE) further down under migration 003.
    ("TRIGGER", "trg_check_preorder_sale_item_is_fungible ON preorder_sales"),
    ("FUNCTION", "check_preorder_sale_item_is_fungible()"),
    ("TABLE", "preorder_sales"),
    # Migration 004 (Milestone 6 — eBay sales CSV import / review queue).
    # DROP TABLE ... CASCADE below removes the new columns/partial unique
    # indexes this migration adds to fungible_depletions/serial_units too
    # — those two tables are already listed further down (migration 003),
    # no separate entry needed for the ALTER TABLE additions themselves.
    ("TABLE", "ebay_sales_row_depletions"),
    ("TABLE", "ebay_sales_rows"),
    ("TABLE", "ebay_import_batches"),
    # Migration 003 (Milestone 5 — sale-side depletion)
    ("TRIGGER", "trg_check_fungible_depletion_not_negative ON fungible_depletions"),
    ("FUNCTION", "check_fungible_depletion_not_negative()"),
    ("TABLE", "fungible_depletions"),
    # Migration 001/002
    ("TRIGGER", "trg_check_purchase_has_lines ON purchases"),
    ("TRIGGER", "trg_check_purchase_reconciliation ON purchase_line_items"),
    ("FUNCTION", "check_purchase_has_lines()"),
    ("FUNCTION", "check_purchase_reconciliation()"),
    ("TABLE", "serial_units"),
    ("TABLE", "purchase_line_items"),
    ("TABLE", "purchases"),
    ("TABLE", "items"),
    ("TABLE", "categories"),
    ("TABLE", "schema_migrations"),
]


class UnsafeDatabaseError(RuntimeError):
    """Raised when a destructive operation (drop_schema) is pointed at a
    database that doesn't look disposable.
    """


def get_engine(database_url: str) -> Engine:
    return create_engine(database_url)


def run_migrations(database_url: str) -> list[str]:
    """Applies every not-yet-applied migration file in MIGRATIONS_DIR, in
    filename order. Returns the list of filenames actually applied.
    """
    engine = get_engine(database_url)
    try:
        return _run_migrations_on_engine(engine)
    finally:
        engine.dispose()


def _run_migrations_on_engine(engine: Engine) -> list[str]:
    applied: list[str] = []
    with engine.begin() as conn:
        conn.execute(text(_ENSURE_TRACKING_TABLE))
        already_applied = {
            row[0] for row in conn.execute(text("SELECT filename FROM schema_migrations"))
        }

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        raise RuntimeError(f"No .sql migration files found in {MIGRATIONS_DIR}")

    for path in migration_files:
        if path.name in already_applied:
            continue
        sql = path.read_text()
        with engine.begin() as conn:
            conn.execute(text(sql))
            conn.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:filename)"),
                {"filename": path.name},
            )
        applied.append(path.name)
    return applied


def drop_schema(engine: Engine) -> None:
    """Drops every object this project's migrations create. TEST-ONLY —
    refuses to run against anything whose database name doesn't contain
    "test" (same guard pattern as Project-Noctrowl's ledger.schema.drop_schema,
    kept independent per-project rather than shared, since these are two
    separate codebases).
    """
    db_name = (urlsplit(str(engine.url)).path or "").lstrip("/")
    if "test" not in db_name.lower():
        raise UnsafeDatabaseError(
            f"Refusing to drop schema on database {db_name!r} — its name "
            "does not contain 'test'. drop_schema() is for disposable test "
            "databases only."
        )

    with engine.begin() as conn:
        for kind, target in _ALL_OBJECTS_NEWEST_FIRST:
            if kind == "TABLE":
                conn.execute(text(f"DROP TABLE IF EXISTS {target} CASCADE"))
            elif kind == "TRIGGER":
                conn.execute(text(f"DROP TRIGGER IF EXISTS {target}"))
            elif kind == "FUNCTION":
                conn.execute(text(f"DROP FUNCTION IF EXISTS {target} CASCADE"))
