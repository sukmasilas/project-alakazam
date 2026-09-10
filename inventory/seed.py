"""Seeds the 5 confirmed categories. Idempotent (ON CONFLICT DO NOTHING) —
safe to call at the start of every test, and would be safe to call at app
startup too, once there is one.

Categories are a data table, not a hardcoded enum (see
migrations/001_initial_schema.sql) specifically so a 6th category is just
an INSERT — this function seeds the 5 confirmed ones from CLAUDE.md /
docs/design/ui-ux-design.md, it doesn't hardcode a closed list anywhere
else in the codebase.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

# (code, display name, SKU prefix) — see design doc's SKU Generator section.
CONFIRMED_CATEGORIES = [
    ("TCG", "TCG", "TCG"),
    ("WATCHES", "Watches", "WATCH"),
    ("AUTOMOTIVE", "Automotive", "AUTO"),
    ("TOYS_COLLECTIBLES", "Toys & Collectibles", "TOY"),
    ("OTHERS", "Others", "OTH"),
]


def seed_categories(conn: Connection) -> None:
    for code, name, sku_prefix in CONFIRMED_CATEGORIES:
        conn.execute(
            text(
                """
                INSERT INTO categories (code, name, sku_prefix)
                VALUES (:code, :name, :sku_prefix)
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {"code": code, "name": name, "sku_prefix": sku_prefix},
        )
