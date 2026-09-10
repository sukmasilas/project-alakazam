"""The purchase-save orchestrator — the real engine behind
docs/design/mockup.html's ``savePurchase``.

Ties together allocation (inventory/allocation.py), SKU/serial generation
(inventory/sku.py, inventory/serials.py), and uniqueness enforcement
(inventory/uniqueness.py) into one transactional operation: validate, then
insert the purchase header, any brand-new items, every line item, and
every serial unit — all inside the caller's DB transaction (this module
never commits/rolls back itself; callers, and tests, own that, same
convention as Project-Noctrowl's ledger/posting.py).

The reconciliation invariant (CLAUDE.md's brief: "This must be enforced as
a real constraint before a purchase is considered postable/committed...
Don't just compute it for display; make it a hard precondition") is
checked HERE, in Python, before a single row is written — a fast, clear
fail-fast layer. The Postgres deferred constraint trigger in
migrations/001_initial_schema.sql re-checks the same thing at COMMIT time
as a structural backstop, exactly mirroring the two-layer pattern
Project-Noctrowl's ledger/posting.py module docstring describes for its own
debits-=-credits invariant.

One deliberate strengthening beyond the mockup: the mockup only ever
*warns* (in the UI) when a serialized line's per-unit costs don't sum to
its line total — it never blocks Save on this. This module treats a
provided (non-default) set of per-unit costs that doesn't sum to the
line's total as a hard ValidationError instead, since letting it through
would silently break the same total-cost integrity the top-level
reconciliation check exists to protect. Flagged here, and in the milestone
report, as an explicit, traceable choice — not a silent deviation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from inventory.allocation import (
    LineInput,
    PurchaseAllocation,
    PurchaseAllocationInput,
    compute_purchase_allocation,
    round_half_up,
    split_serial_unit_costs,
)
from inventory.exceptions import (
    DuplicateSerialError,
    DuplicateSkuError,
    ReconciliationError,
    ValidationError,
)
from inventory.queries import (
    count_serial_units_for_item,
    get_category_by_code,
    get_item_by_sku,
    get_skus_for_prefix_slug,
)
from inventory.serials import generate_serials_for_line
from inventory.sku import generate_sku
from inventory.uniqueness import find_duplicate_keys, serial_exists, sku_exists

IDENTITY_MODES = ("fungible", "serialized")
PRICING_MODES = ("direct", "lumpsum_group")
PRICE_ENTRY_MODES = ("per_unit", "total")
SHIPPING_MODES = ("none", "manual", "pooled")
SPLIT_METHODS = ("equal", "by_weight", "by_value")


@dataclass
class NewItemInput:
    name: str
    category_code: str
    identity_mode: str  # 'fungible' | 'serialized'
    sku: Optional[str] = None  # None => auto-generate


@dataclass
class SerialUnitInput:
    serial_id: Optional[str] = None  # None => auto-generate
    cost: Optional[Decimal] = None  # None => participate in the default even split
    photo_reference: Optional[str] = None


@dataclass
class PurchaseLineInput:
    sku: Optional[str] = None  # exactly one of sku / new_item must be set
    new_item: Optional[NewItemInput] = None
    quantity: int = 1

    pricing_mode: str = "direct"  # 'direct' | 'lumpsum_group'
    price_entry_mode: Optional[str] = None  # required if pricing_mode == 'direct'
    price_value: Optional[Decimal] = None  # required if pricing_mode == 'direct'
    lumpsum_weight_kg: Optional[Decimal] = None
    lumpsum_value: Optional[Decimal] = None

    ships_separately: bool = False
    manual_shipping_amount: Optional[Decimal] = None
    shipping_weight_kg: Optional[Decimal] = None

    # Required (length == quantity) if this line's resolved item is
    # serialized. Any unit may omit serial_id/cost to be auto-generated /
    # default-split.
    serial_units: Optional[list[SerialUnitInput]] = None


@dataclass
class PurchaseInput:
    purchase_date: date
    vendor_description: str
    total_amount_paid: Decimal
    currency: str = "IDR"
    fx_rate_to_idr: Optional[Decimal] = None

    shipping_mode: str = "none"
    pooled_shipping_total: Optional[Decimal] = None
    pooled_shipping_method: Optional[str] = None

    lump_sum_active: bool = False
    lump_sum_total: Optional[Decimal] = None
    lump_sum_method: Optional[str] = None

    invoice_document_ref: Optional[str] = None
    invoice_ocr_status: Optional[str] = None
    invoice_parsed_fields: Optional[dict] = None

    lines: list[PurchaseLineInput] = field(default_factory=list)


@dataclass
class SavedLine:
    purchase_line_item_id: int
    item_id: int
    sku: str
    allocated_item_cost: int
    shipping_share: int
    line_total: int
    serial_ids: list[str] = field(default_factory=list)


@dataclass
class SavedPurchase:
    id: int
    purchase_ref: str
    allocation: PurchaseAllocation
    lines: list[SavedLine]


def _validate_structure(purchase: PurchaseInput) -> None:
    if not purchase.lines:
        raise ValidationError("A purchase must have at least one line item.")
    if purchase.shipping_mode not in SHIPPING_MODES:
        raise ValidationError(f"Unknown shipping_mode: {purchase.shipping_mode!r}")
    if purchase.shipping_mode == "pooled":
        if purchase.pooled_shipping_total is None or purchase.pooled_shipping_method not in SPLIT_METHODS:
            raise ValidationError(
                "shipping_mode='pooled' requires pooled_shipping_total and a valid pooled_shipping_method."
            )
    if purchase.lump_sum_active:
        if purchase.lump_sum_total is None or purchase.lump_sum_method not in SPLIT_METHODS:
            raise ValidationError(
                "lump_sum_active requires lump_sum_total and a valid lump_sum_method."
            )

    lumpsum_line_count = 0
    for i, line in enumerate(purchase.lines):
        if (line.sku is None) == (line.new_item is None):
            raise ValidationError(f"Line {i}: exactly one of sku / new_item must be set.")
        if line.quantity <= 0:
            raise ValidationError(f"Line {i}: quantity must be positive.")
        if line.new_item is not None and line.new_item.identity_mode not in IDENTITY_MODES:
            raise ValidationError(f"Line {i}: unknown identity_mode {line.new_item.identity_mode!r}.")
        if line.pricing_mode not in PRICING_MODES:
            raise ValidationError(f"Line {i}: unknown pricing_mode {line.pricing_mode!r}.")
        if line.pricing_mode == "direct":
            if line.price_entry_mode not in PRICE_ENTRY_MODES or line.price_value is None:
                raise ValidationError(
                    f"Line {i}: pricing_mode='direct' requires price_entry_mode and price_value."
                )
        else:  # lumpsum_group
            lumpsum_line_count += 1
            if not purchase.lump_sum_active:
                raise ValidationError(
                    f"Line {i}: pricing_mode='lumpsum_group' but the purchase has no active lump-sum group."
                )

    if purchase.lump_sum_active and lumpsum_line_count == 0:
        raise ValidationError("lump_sum_active is set but no line uses pricing_mode='lumpsum_group'.")


def _resolve_new_item_sku(
    conn: Connection, category_prefix: str, name: str, explicit_sku: Optional[str], reserved: list[str]
) -> str:
    if explicit_sku:
        return explicit_sku.strip().upper()
    # existing_skus for generation = every DB item sharing this prefix/slug
    # base, plus every SKU already reserved earlier in this same purchase.
    from inventory.sku import slugify_name  # local import: keeps sku.py DB-free

    base = f"{category_prefix}-{slugify_name(name)}"
    existing = get_skus_for_prefix_slug(conn, base) + reserved
    return generate_sku(category_prefix, name, existing)


def _generate_purchase_ref(conn: Connection, purchase_date: date) -> str:
    year = purchase_date.year
    rows = conn.execute(
        text("SELECT purchase_ref FROM purchases WHERE purchase_ref LIKE :pattern"),
        {"pattern": f"PUR-{year}-%"},
    )
    max_seq = 0
    prefix = f"PUR-{year}-"
    for (ref,) in rows:
        suffix = ref[len(prefix):]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))
    return f"{prefix}{max_seq + 1:04d}"


def save_purchase(conn: Connection, purchase: PurchaseInput) -> SavedPurchase:
    _validate_structure(purchase)

    # --- Resolve each line's item (existing lookup, or new-item creation
    # plan) and identity mode, without touching the DB's items table yet. ---
    resolved_new_skus: list[str] = []
    line_plans: list[dict] = []  # per-line resolution state, built up as we go

    for i, line in enumerate(purchase.lines):
        if line.sku is not None:
            item = get_item_by_sku(conn, line.sku)
            if item is None:
                raise ValidationError(f"Line {i}: no existing item with SKU {line.sku!r}.")
            line_plans.append({"mode": "existing", "item": item, "sku": item.sku})
        else:
            category = get_category_by_code(conn, line.new_item.category_code)
            if category is None:
                raise ValidationError(
                    f"Line {i}: unknown category_code {line.new_item.category_code!r}."
                )
            sku = _resolve_new_item_sku(
                conn, category.sku_prefix, line.new_item.name, line.new_item.sku, resolved_new_skus
            )
            resolved_new_skus.append(sku)
            line_plans.append(
                {
                    "mode": "new",
                    "sku": sku,
                    "name": line.new_item.name,
                    "category_id": category.id,
                    "identity_mode": line.new_item.identity_mode,
                }
            )

    # --- SKU uniqueness: batch check across this purchase's new items,
    # THEN a real DB check against everything already posted. ---
    dupes = find_duplicate_keys(resolved_new_skus)
    if dupes:
        raise DuplicateSkuError(next(iter(dupes)))
    for sku in resolved_new_skus:
        if sku_exists(conn, sku):
            raise DuplicateSkuError(sku)

    # --- Reconciliation: the hard precondition, checked in Python before
    # any row is written (see module docstring). ---
    allocation_input = PurchaseAllocationInput(
        total_amount_paid=purchase.total_amount_paid,
        shipping_mode=purchase.shipping_mode,
        pooled_shipping_total=purchase.pooled_shipping_total,
        pooled_shipping_method=purchase.pooled_shipping_method,
        lump_sum_active=purchase.lump_sum_active,
        lump_sum_total=purchase.lump_sum_total,
        lump_sum_method=purchase.lump_sum_method,
        lines=[
            LineInput(
                key=i,
                quantity=line.quantity,
                pricing_mode=line.pricing_mode,
                price_entry_mode=line.price_entry_mode,
                price_value=line.price_value,
                lumpsum_weight_kg=line.lumpsum_weight_kg,
                lumpsum_value=line.lumpsum_value,
                ships_separately=line.ships_separately,
                manual_shipping_amount=line.manual_shipping_amount,
                shipping_weight_kg=line.shipping_weight_kg,
            )
            for i, line in enumerate(purchase.lines)
        ],
    )
    allocation = compute_purchase_allocation(allocation_input)
    if not allocation.balanced:
        raise ReconciliationError(allocation.running_total, allocation.total_amount_paid)

    # --- Insert brand-new items now that everything above has passed. ---
    for plan in line_plans:
        if plan["mode"] != "new":
            continue
        try:
            # A savepoint, not a plain execute: the pre-check above
            # (sku_exists) only catches the common case. Under genuine
            # concurrency (two callers both pass the pre-check, then race
            # on the real INSERT), this unique-index violation is the
            # actual backstop — and without a savepoint here, catching the
            # resulting IntegrityError leaves the whole caller-supplied
            # connection hard-aborted in Postgres (unusable until an
            # external rollback), even though the raised DuplicateSkuError
            # looks identical to the much more common pre-check path. The
            # savepoint scopes the abort to just this statement, so the
            # exception is safe for a caller to catch and recover from
            # (rollback/retry) without losing the rest of the transaction.
            with conn.begin_nested():
                item_id = conn.execute(
                    text(
                        "INSERT INTO items (sku, name, category_id, identity_mode) "
                        "VALUES (:sku, :name, :category_id, :identity_mode) RETURNING id"
                    ),
                    {
                        "sku": plan["sku"],
                        "name": plan["name"],
                        "category_id": plan["category_id"],
                        "identity_mode": plan["identity_mode"],
                    },
                ).scalar_one()
        except IntegrityError as exc:
            raise DuplicateSkuError(plan["sku"]) from exc
        plan["item_id"] = item_id
        plan["item_identity_mode"] = plan["identity_mode"]

    for plan in line_plans:
        if plan["mode"] == "existing":
            plan["item_id"] = plan["item"].id
            plan["item_identity_mode"] = plan["item"].identity_mode

    # --- Resolve serial units for serialized lines: real serial IDs (given
    # or generated) and per-unit costs (given or default-split), tracking
    # every SKU's assigned serials across lines within this same purchase
    # so sibling lines of one SKU never collide with each other. ---
    assigned_serials_by_sku: dict[str, list[str]] = {}
    for i, (line, plan) in enumerate(zip(purchase.lines, line_plans)):
        plan["resolved_serials"] = None
        plan["resolved_costs"] = None
        plan["resolved_photos"] = None
        if plan["item_identity_mode"] != "serialized":
            continue

        provided = line.serial_units or []
        if provided and len(provided) != line.quantity:
            raise ValidationError(
                f"Line {i}: {len(provided)} serial unit(s) provided but quantity is {line.quantity}."
            )
        if not provided:
            provided = [SerialUnitInput() for _ in range(line.quantity)]

        sku = plan["sku"]
        existing_count = count_serial_units_for_item(conn, plan["item_id"])
        sibling_reserved = list(assigned_serials_by_sku.get(sku, []))
        explicit_serials = [u.serial_id.strip() for u in provided if u.serial_id]
        sibling_reserved += explicit_serials

        resolved_serials: list[Optional[str]] = [
            u.serial_id.strip() if u.serial_id else None for u in provided
        ]
        missing_count = sum(1 for s in resolved_serials if s is None)
        if missing_count:
            generated = iter(
                generate_serials_for_line(sku, missing_count, existing_count, sibling_reserved)
            )
            resolved_serials = [s if s is not None else next(generated) for s in resolved_serials]

        assigned_serials_by_sku.setdefault(sku, []).extend(resolved_serials)

        line_total = allocation.line_by_key(i).line_total
        provided_costs = [u.cost for u in provided]
        if all(c is None for c in provided_costs):
            costs = split_serial_unit_costs(line_total, line.quantity)
        elif any(c is None for c in provided_costs):
            raise ValidationError(
                f"Line {i}: specify a cost for every serial unit, or none (to use the default even split)."
            )
        else:
            costs = [round_half_up(c) for c in provided_costs]
            if sum(costs) != line_total:
                raise ValidationError(
                    f"Line {i}: serial unit costs sum to {sum(costs)}, but the line's total is {line_total}."
                )

        plan["resolved_serials"] = resolved_serials
        plan["resolved_costs"] = costs
        plan["resolved_photos"] = [u.photo_reference for u in provided]

    # --- Serial ID uniqueness: batch check across this purchase, then a
    # real DB check against everything already posted. ---
    all_new_serials = [s for plan in line_plans for s in (plan["resolved_serials"] or [])]
    dupes = find_duplicate_keys(all_new_serials)
    if dupes:
        raise DuplicateSerialError(next(iter(dupes)))
    for serial in all_new_serials:
        if serial_exists(conn, serial):
            raise DuplicateSerialError(serial)

    # --- Insert the purchase header. ---
    purchase_ref = _generate_purchase_ref(conn, purchase.purchase_date)
    purchase_id = conn.execute(
        text(
            """
            INSERT INTO purchases (
                purchase_ref, purchase_date, vendor_description, total_amount_paid,
                currency, fx_rate_to_idr, shipping_mode, pooled_shipping_total,
                pooled_shipping_method, lump_sum_active, lump_sum_total, lump_sum_method,
                invoice_document_ref, invoice_ocr_status, invoice_parsed_fields
            ) VALUES (
                :purchase_ref, :purchase_date, :vendor_description, :total_amount_paid,
                :currency, :fx_rate_to_idr, :shipping_mode, :pooled_shipping_total,
                :pooled_shipping_method, :lump_sum_active, :lump_sum_total, :lump_sum_method,
                :invoice_document_ref, :invoice_ocr_status, CAST(:invoice_parsed_fields AS JSONB)
            ) RETURNING id
            """
        ),
        {
            "purchase_ref": purchase_ref,
            "purchase_date": purchase.purchase_date,
            "vendor_description": purchase.vendor_description,
            "total_amount_paid": allocation.total_amount_paid,
            "currency": purchase.currency,
            "fx_rate_to_idr": purchase.fx_rate_to_idr,
            "shipping_mode": purchase.shipping_mode,
            "pooled_shipping_total": purchase.pooled_shipping_total,
            "pooled_shipping_method": purchase.pooled_shipping_method,
            "lump_sum_active": purchase.lump_sum_active,
            "lump_sum_total": purchase.lump_sum_total,
            "lump_sum_method": purchase.lump_sum_method,
            "invoice_document_ref": purchase.invoice_document_ref,
            "invoice_ocr_status": purchase.invoice_ocr_status,
            "invoice_parsed_fields": (
                None if purchase.invoice_parsed_fields is None else json.dumps(purchase.invoice_parsed_fields)
            ),
        },
    ).scalar_one()

    saved_lines: list[SavedLine] = []
    for i, (line, plan) in enumerate(zip(purchase.lines, line_plans)):
        line_alloc = allocation.line_by_key(i)
        line_item_id = conn.execute(
            text(
                """
                INSERT INTO purchase_line_items (
                    purchase_id, item_id, quantity, pricing_mode, price_entry_mode, price_value,
                    lumpsum_weight_kg, lumpsum_value, ships_separately, manual_shipping_amount,
                    shipping_weight_kg, allocated_item_cost, shipping_share, line_total
                ) VALUES (
                    :purchase_id, :item_id, :quantity, :pricing_mode, :price_entry_mode, :price_value,
                    :lumpsum_weight_kg, :lumpsum_value, :ships_separately, :manual_shipping_amount,
                    :shipping_weight_kg, :allocated_item_cost, :shipping_share, :line_total
                ) RETURNING id
                """
            ),
            {
                "purchase_id": purchase_id,
                "item_id": plan["item_id"],
                "quantity": line.quantity,
                "pricing_mode": line.pricing_mode,
                "price_entry_mode": line.price_entry_mode if line.pricing_mode == "direct" else None,
                "price_value": line.price_value if line.pricing_mode == "direct" else None,
                "lumpsum_weight_kg": line.lumpsum_weight_kg if line.pricing_mode == "lumpsum_group" else None,
                "lumpsum_value": line.lumpsum_value if line.pricing_mode == "lumpsum_group" else None,
                "ships_separately": line.ships_separately,
                "manual_shipping_amount": line.manual_shipping_amount,
                "shipping_weight_kg": line.shipping_weight_kg,
                "allocated_item_cost": line_alloc.allocated_item_cost,
                "shipping_share": line_alloc.shipping_share,
                "line_total": line_alloc.line_total,
            },
        ).scalar_one()

        serial_ids: list[str] = []
        if plan["resolved_serials"] is not None:
            for serial_id, cost, photo_ref in zip(
                plan["resolved_serials"], plan["resolved_costs"], plan["resolved_photos"]
            ):
                try:
                    # See the matching comment on the new-item insert above:
                    # a savepoint keeps a genuine-concurrency unique-index
                    # violation here from hard-aborting the caller's whole
                    # connection, so DuplicateSerialError is equally safe
                    # to catch and recover from regardless of whether the
                    # pre-check or the real constraint is what caught it.
                    with conn.begin_nested():
                        conn.execute(
                            text(
                                """
                                INSERT INTO serial_units (
                                    item_id, serial_id, acquired_cost, purchase_line_item_id, photo_reference
                                ) VALUES (:item_id, :serial_id, :acquired_cost, :purchase_line_item_id, :photo_reference)
                                """
                            ),
                            {
                                "item_id": plan["item_id"],
                                "serial_id": serial_id,
                                "acquired_cost": cost,
                                "purchase_line_item_id": line_item_id,
                                "photo_reference": photo_ref,
                            },
                        )
                except IntegrityError as exc:
                    raise DuplicateSerialError(serial_id) from exc
                serial_ids.append(serial_id)

        saved_lines.append(
            SavedLine(
                purchase_line_item_id=line_item_id,
                item_id=plan["item_id"],
                sku=plan["sku"],
                allocated_item_cost=line_alloc.allocated_item_cost,
                shipping_share=line_alloc.shipping_share,
                line_total=line_alloc.line_total,
                serial_ids=serial_ids,
            )
        )

    return SavedPurchase(id=purchase_id, purchase_ref=purchase_ref, allocation=allocation, lines=saved_lines)
