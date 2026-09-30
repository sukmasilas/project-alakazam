"""Opening inventory entry — a simpler way to record inventory the user
already physically has, without needing real historical purchase detail
(vendor, date, shipping cost, allocation method). See CLAUDE.md's "Opening
inventory entry, decision confirmed 2026-09-30" and "Readiness gaps for real
data entry, identified and being closed, 2026-09-30".

## Core design principle — this IS a real Purchase, not a new concept

An opening-inventory entry is a thin orchestration layer over the existing,
UNMODIFIED ``inventory.purchases.save_purchase()`` — always with
``pricing_mode="direct"`` and ``shipping_mode="none"`` (no lump-sum, no
pooled/manual shipping; none of that complexity applies to onboarding stock
the user already owns). This means the exact same cost-basis and
reconciliation invariants already proven since Milestone 2 apply
automatically, with ZERO new schema and ZERO new invariant needed — the same
"thin orchestration layer over the existing engine" discipline already
established by ``inventory/preorders.py::fulfill_preorder_sales`` and
``inventory/consignment.py::intake_consigned_units``.

One opening-inventory entry = one Purchase with exactly one line (one item,
one quantity, one total/per-unit value — see CLAUDE.md's brief: "User
enters: an item..., a quantity, and a total value"). A user onboarding many
different existing items submits this multiple times — there is
deliberately NO idempotency/uniqueness constraint beyond what
``save_purchase()`` already enforces (SKU/serial uniqueness), unlike
Project-Noctrowl's one-time-per-wallet opening balance: a user may have many
distinct items to onboard this way over time.

## Vendor/description default — a plain, editable text field, not new schema

The purchase's ``vendor_description`` defaults to ``DEFAULT_VENDOR_LABEL``
("Opening Inventory") when the caller leaves it blank, but stays a plain,
user-editable text field on the real ``purchases`` table — no new
boolean/tag column is added. This is what makes an opening-inventory entry
visibly distinguishable later in Purchase History (e.g. by searching/
filtering on that text) without any schema change.

## Reconciliation is guaranteed by construction, not re-derived here

The header's ``total_amount_paid`` is computed with the EXACT SAME formula
``inventory.allocation.compute_purchase_allocation`` uses for a single
direct-priced line with no shipping (``round_half_up(price_value)`` for
``price_entry_mode="total"``, ``round_half_up(price_value * quantity)`` for
``"per_unit"``) — so the one-line purchase this module builds always
reconciles exactly, by construction, without needing to call the allocation
engine directly. ``save_purchase()`` still independently re-validates this
(the real, authoritative reconciliation check) — this module never bypasses
it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from sqlalchemy.engine import Connection

from inventory.allocation import round_half_up
from inventory.exceptions import ValidationError
from inventory.purchases import (
    NewItemInput,
    PurchaseInput,
    PurchaseLineInput,
    SavedPurchase,
    SerialUnitInput,
    save_purchase,
)

DEFAULT_VENDOR_LABEL = "Opening Inventory"

PRICE_ENTRY_MODES = ("per_unit", "total")


@dataclass
class OpeningInventorySerialUnitInput:
    serial_id: Optional[str] = None  # None => auto-generate
    cost: Optional[Decimal] = None  # None => participate in the default even split


@dataclass
class OpeningInventoryInput:
    quantity: int = 1
    price_entry_mode: str = "total"  # 'per_unit' | 'total'
    price_value: Decimal = Decimal("0")

    # Exactly one of the two below must be set — same convention as
    # inventory.purchases.PurchaseLineInput.
    sku: Optional[str] = None
    new_item: Optional[NewItemInput] = None

    entry_date: Optional[date_type] = None
    # None/blank => DEFAULT_VENDOR_LABEL. Always a plain, editable string —
    # never a separate flag (see module docstring).
    vendor_description: Optional[str] = None

    # Required (length == quantity) only for a serialized item with
    # quantity > 1 that needs explicit per-unit serial IDs/costs — same
    # convention as inventory.purchases.PurchaseLineInput.serial_units;
    # any unit may omit serial_id/cost to be auto-generated/default-split.
    serial_units: Optional[list[OpeningInventorySerialUnitInput]] = None


def record_opening_inventory(conn: Connection, entry: OpeningInventoryInput) -> SavedPurchase:
    """Records one opening-inventory entry as a real Purchase (one line,
    direct pricing, no shipping) via the unmodified ``save_purchase()``.
    Every structural validation (exactly one of sku/new_item, serialized
    unit assignment, SKU/serial uniqueness, quantity > 0) is enforced by
    ``save_purchase()`` itself — this function does not duplicate any of
    that; it only resolves the two opening-inventory-specific conveniences
    (the vendor-description default, and computing ``total_amount_paid``
    from the single line so the purchase reconciles by construction).
    """
    if entry.price_entry_mode not in PRICE_ENTRY_MODES:
        raise ValidationError(f"Unknown price_entry_mode: {entry.price_entry_mode!r}")

    quantity = entry.quantity
    if quantity is None or not isinstance(quantity, int) or quantity <= 0:
        raise ValidationError("Opening inventory quantity must be a positive integer.")

    price_value = Decimal(str(entry.price_value)) if entry.price_value is not None else Decimal(0)
    if entry.price_entry_mode == "per_unit":
        total_amount_paid = round_half_up(price_value * quantity)
    else:
        total_amount_paid = round_half_up(price_value)

    vendor_description = (entry.vendor_description or "").strip() or DEFAULT_VENDOR_LABEL

    serial_units = None
    if entry.serial_units is not None:
        serial_units = [
            SerialUnitInput(serial_id=u.serial_id, cost=u.cost) for u in entry.serial_units
        ]

    purchase = PurchaseInput(
        purchase_date=entry.entry_date or date_type.today(),
        vendor_description=vendor_description,
        total_amount_paid=Decimal(total_amount_paid),
        currency="IDR",
        shipping_mode="none",
        lump_sum_active=False,
        lines=[
            PurchaseLineInput(
                sku=entry.sku,
                new_item=entry.new_item,
                quantity=quantity,
                pricing_mode="direct",
                price_entry_mode=entry.price_entry_mode,
                price_value=price_value,
                serial_units=serial_units,
            )
        ],
    )
    return save_purchase(conn, purchase)
