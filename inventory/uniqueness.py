"""Real uniqueness enforcement — the actual gate that blocks a duplicate
SKU or Serial/Asset ID from being saved, independent of whatever generated
the candidate value.

This is the fix for the real bug CLAUDE.md's brief calls out: the mockup's
generators (inventory/sku.py, inventory/serials.py) avoid collisions in the
common case, but nothing actually blocked a save if a duplicate arose
anyway. Two independent layers exist here, matching the brief exactly:

1. ``find_duplicate_keys`` — a pure, in-memory check across a *batch* of
   candidate values (e.g. every new-SKU line in one in-flight purchase),
   so two lines can't silently create the same duplicate together before
   either has touched the database.
2. ``sku_exists`` / ``serial_exists`` — real database lookups against the
   unique-indexed columns (see migrations/001_initial_schema.sql), the
   actual backstop against a value that collides with something already
   posted. inventory/purchases.py calls both layers before insert, AND
   still relies on the DB's own unique index to catch a race condition
   (translated into the same DuplicateSkuError/DuplicateSerialError via a
   caught IntegrityError) — belt and suspenders, not either/or.

Normalization: both layers compare case-insensitively (SKUs/serials are
stored as typed, but two differently-cased entries of the same code are
still the same real-world identifier) — matches the mockup's own
``.toUpperCase()`` comparisons in ``isSkuTaken`` / ``findDuplicateSerialKeys``.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection


def _normalize(value: str) -> str:
    return (value or "").strip().upper()


def find_duplicate_keys(values: Iterable[str]) -> set[str]:
    """Returns the set of normalized values that appear more than once in
    ``values`` (blank/whitespace-only values are ignored — an unset code
    isn't a "duplicate" of another unset code).
    """
    counts: dict[str, int] = {}
    for value in values:
        norm = _normalize(value)
        if not norm:
            continue
        counts[norm] = counts.get(norm, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def sku_exists(conn: Connection, sku: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM items WHERE UPPER(sku) = :sku LIMIT 1"),
        {"sku": _normalize(sku)},
    ).first()
    return row is not None


def serial_exists(conn: Connection, serial_id: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM serial_units WHERE UPPER(serial_id) = :serial_id LIMIT 1"),
        {"serial_id": _normalize(serial_id)},
    ).first()
    return row is not None
