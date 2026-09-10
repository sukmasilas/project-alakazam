"""Standalone item creation — the real engine behind the Inventory screen's
"+ Add New Item" panel (Milestone 3, web app).

This is a genuinely different path from a purchase line's inline new-item
creation in ``inventory/purchases.py::save_purchase()``: a purchase always
requires at least one line item (enforced by the ``check_purchase_has_lines``
DB trigger), so this path creates a **zero-quantity, zero-cost** item record
with no purchase at all (ui-ux-design.md: "a pre-registered SKU with zero
on-hand qty/cost basis, ready to be picked up whenever it's actually
purchased through Purchase Entry").

Reuses the exact same SKU-generation (``inventory/sku.py``) and uniqueness
(``inventory/uniqueness.py``) primitives ``save_purchase()`` itself uses —
this module does not reimplement either, and applies the same two-layer
uniqueness pattern (pre-check, then a savepoint-scoped real INSERT so a
genuine race is still caught by the DB's own unique index without hard-
aborting the caller's transaction).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from inventory.exceptions import DuplicateSkuError, ValidationError
from inventory.queries import Item, get_category_by_code, get_skus_for_prefix_slug
from inventory.sku import generate_sku, slugify_name
from inventory.uniqueness import sku_exists

IDENTITY_MODES = ("fungible", "serialized")


def create_item(
    conn: Connection,
    name: str,
    category_code: str,
    identity_mode: str,
    sku: Optional[str] = None,
) -> Item:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Item name is required.")
    if identity_mode not in IDENTITY_MODES:
        raise ValidationError(f"Unknown identity_mode: {identity_mode!r}")

    category = get_category_by_code(conn, category_code)
    if category is None:
        raise ValidationError(f"Unknown category_code: {category_code!r}")

    if sku and sku.strip():
        resolved_sku = sku.strip().upper()
    else:
        base = f"{category.sku_prefix}-{slugify_name(name)}"
        resolved_sku = generate_sku(category.sku_prefix, name, get_skus_for_prefix_slug(conn, base))

    if sku_exists(conn, resolved_sku):
        raise DuplicateSkuError(resolved_sku)

    try:
        # Savepoint, not a plain execute — same reasoning as
        # save_purchase()'s new-item insert: under genuine concurrency the
        # pre-check above can race, and the real unique index is the actual
        # backstop. Scoping the abort to a savepoint keeps the exception
        # safe for the caller to catch without losing the whole transaction.
        with conn.begin_nested():
            item_id = conn.execute(
                text(
                    "INSERT INTO items (sku, name, category_id, identity_mode) "
                    "VALUES (:sku, :name, :category_id, :identity_mode) RETURNING id"
                ),
                {
                    "sku": resolved_sku,
                    "name": name,
                    "category_id": category.id,
                    "identity_mode": identity_mode,
                },
            ).scalar_one()
    except IntegrityError as exc:
        raise DuplicateSkuError(resolved_sku) from exc

    return Item(id=item_id, sku=resolved_sku, name=name, category_id=category.id, identity_mode=identity_mode)
