"""Location (shelving) tracking — confirmed 2026-10-01.

Records where each item is physically stored. See CLAUDE.md's "Shelving/
location feature, confirmed 2026-10-01" for the full scope:

- One location PER ITEM — a single field on the item, not per-serialized-
  unit, not split across multiple locations for fungible stock.
- A structured, admin-editable list of locations, not free text — mirrors
  ``inventory/consignment.py``'s Consignor CRUD shape (``create_consignor``/
  ``list_consignors``) exactly: simple create + list, no deletion for v1,
  same precedent.
- Editable at two points: settable inline whenever a new item is created
  (``inventory/purchases.py::save_purchase()`` — used by both Purchase Entry
  and Opening Inventory, ``inventory/consignment.py::intake_consigned_units()``,
  and the standalone ``inventory/items.py::create_item()`` behind the
  Inventory screen's own "+ Add New Item" panel — see that module's report
  for why this is a genuinely fourth, separate insertion point), and
  editable afterward via ``set_item_location()`` below (Item Detail's
  "change location" action) for an item that physically moves later.
- Not a money- or stock-correctness field — no DB trigger/invariant needed
  the way consignment's ``consignor_id`` needed one (see migrations/
  007_add_locations.sql). ``location_id`` is NOT write-once, unlike
  ``consignor_id``/``identity_mode`` — it is expected to be updated freely.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from inventory.exceptions import DuplicateLocationNameError, ValidationError


@dataclass
class Location:
    id: int
    name: str


def create_location(conn: Connection, name: str) -> Location:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Location name is required.")
    try:
        # Savepoint, not a plain execute — same reasoning as every other
        # uniqueness-backed insert in this codebase (inventory/items.py's
        # create_item, inventory/consignment.py's create_consignor's sibling
        # patterns): a genuine race against the real unique index is safe
        # for the caller to catch and recover from without losing the rest
        # of the transaction.
        with conn.begin_nested():
            row_id = conn.execute(
                text("INSERT INTO locations (name) VALUES (:name) RETURNING id"),
                {"name": name},
            ).scalar_one()
    except IntegrityError as exc:
        raise DuplicateLocationNameError(name) from exc
    return Location(id=row_id, name=name)


def list_locations(conn: Connection) -> list[Location]:
    rows = conn.execute(
        text("SELECT id, name FROM locations ORDER BY name")
    ).mappings().all()
    return [Location(**row) for row in rows]


def get_location(conn: Connection, location_id: int) -> Optional[Location]:
    row = conn.execute(
        text("SELECT id, name FROM locations WHERE id = :id"),
        {"id": location_id},
    ).mappings().first()
    return Location(**row) if row else None


def validate_location_id(conn: Connection, location_id: Optional[int]) -> None:
    """Real, up-front pre-check for every item-creation path that accepts an
    optional ``location_id`` — mirrors the existing
    ``get_category_by_code``-then-``ValidationError`` pattern
    (``inventory/purchases.py``, ``inventory/items.py``,
    ``inventory/consignment.py``) rather than letting an invalid id surface
    as a raw FK-violation ``IntegrityError`` from inside a savepoint that's
    already catching a DIFFERENT specific error (e.g. ``DuplicateSkuError``)
    — which would otherwise mis-attribute a bad ``location_id`` as a SKU
    collision. A no-op when ``location_id`` is ``None`` (never required).
    """
    if location_id is None:
        return
    if get_location(conn, location_id) is None:
        raise ValidationError(f"No location with id {location_id!r}.")


def set_item_location(conn: Connection, sku: str, location_id: Optional[int]) -> None:
    """Updates an existing item's location by SKU — the "editable afterward
    from Item Detail" requirement. ``location_id=None`` clears it back to
    unset (never required — see module docstring).
    """
    validate_location_id(conn, location_id)
    result = conn.execute(
        text(
            "UPDATE items SET location_id = :location_id WHERE UPPER(sku) = UPPER(:sku) RETURNING id"
        ),
        {"location_id": location_id, "sku": sku},
    ).first()
    if result is None:
        raise ValidationError(f"No item with SKU {sku!r}.")
