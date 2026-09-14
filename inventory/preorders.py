"""Pre-order/dropship sales — Milestone 7.

Every prior milestone (Purchases, Depletions, eBay import) assumes a
purchase/acquisition already happened before anything else occurs.
Pre-order inverts this: the SALE happens first, before any purchase record
exists for the item — the seller only buys/ships the item after it sells.
COGS/cost basis is only knowable once the purchase actually happens
(fulfillment time), not at sale time. See CLAUDE.md's "Milestone 7
decisions, confirmed 2026-09-14" for the full scope and rationale.

Fungible items only in this first pass — serialized pre-order (no unit/
serial identity exists until the item is actually acquired) is deferred.
No sale price/revenue field anywhere — same standing rule as every prior
milestone; Alakazam tracks inventory/cost movement only.

## Design shape — reuses existing engines rather than reinventing them

Fulfillment = a normal Purchase (via the existing, UNMODIFIED
``inventory/purchases.py::save_purchase()``) immediately followed by one
``inventory.depletions.deplete_fungible()`` call per pre-order sale being
fulfilled (extended with one more optional, backward-compatible
``preorder_sale_id`` parameter — same pattern as Milestone 6's
``ebay_transaction_id`` addition). This is the mathematically correct
treatment, not an approximation: the purchase's real cost genuinely joins
the weighted-average cost pool (``get_item_stats`` is read fresh, after the
purchase, by ``deplete_fungible``) before the depletion(s) draw from it,
and any purchased quantity beyond what's needed to cover the pending
pre-orders being fulfilled correctly remains as genuine on-hand stock
afterward.

Critically: the existing negative-stock invariant (already proven safe
under real concurrency in Milestone 5 — see migrations/
003_add_depletions.sql) automatically protects this new flow for free. No
new concurrency-sensitive invariant is needed for the purchase+deplete
half of this milestone.

## The one genuinely new invariant: the status transition

``preorder_sales.status`` (pending -> fulfilled / pending -> cancelled)
uses a plain atomic ``UPDATE ... WHERE status = 'pending'`` compare-and-
swap in BOTH ``cancel_preorder_sale`` and ``fulfill_preorder_sales`` below
— mirroring Milestone 6's row-confirmation gate
(``ingestion/ebay_import.py::process_confirmed_rows``) and Milestone 5's
serialized-unit depletion path, NEVER the trigger-based
``SELECT ... FOR UPDATE`` pattern that caused a real, reproducible deadlock
in Milestone 5's original fungible-depletion design (see
migrations/003_add_depletions.sql's own trigger comment for the full
root-cause analysis). A plain single-statement compare-and-swap UPDATE
relies on Postgres's own row-level MVCC locking — two concurrent UPDATEs
targeting the same row serialize on the row itself, so two "fulfill/cancel
this pre-order" requests can never both succeed, and there's no
lock-upgrade sequence for a deadlock to form over.

Both ``pending -> fulfilled`` and ``pending -> cancelled`` are TERMINAL —
no function here ever moves a row back out of either state (same
"corrections to a posted/fulfilled row are out of scope for this
prototype" policy already carried through Purchases, Depletions, and eBay
import). Cancelling a still-pending row is NOT a correction of a posted
fact — nothing has touched inventory/cost for a pending pre-order yet — so
this does not reopen that gap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from inventory.depletions import FungibleDepletionResult, deplete_fungible
from inventory.exceptions import (
    PreorderItemMismatchError,
    PreorderSaleNotFoundError,
    PreorderSaleNotPendingError,
    ValidationError,
)
from inventory.purchases import PurchaseInput, SavedPurchase, save_purchase
from inventory.queries import get_item_by_sku

_STATUSES = ("pending", "fulfilled", "cancelled")


@dataclass
class PreorderSaleResult:
    id: int
    item_id: int
    sku: str
    item_name: str
    quantity: int
    sale_date: date_type
    reference: Optional[str]
    status: str


_SELECT_PREORDER_SALE = """
    SELECT ps.id, ps.item_id, i.sku, i.name AS item_name, ps.quantity,
           ps.sale_date, ps.reference, ps.status
    FROM preorder_sales ps
    JOIN items i ON i.id = ps.item_id
"""


def _row_to_result(row) -> PreorderSaleResult:
    return PreorderSaleResult(
        id=row["id"],
        item_id=row["item_id"],
        sku=row["sku"],
        item_name=row["item_name"],
        quantity=row["quantity"],
        sale_date=row["sale_date"],
        reference=row["reference"],
        status=row["status"],
    )


def get_preorder_sale(conn: Connection, preorder_sale_id: int) -> PreorderSaleResult:
    row = conn.execute(
        text(_SELECT_PREORDER_SALE + " WHERE ps.id = :id"), {"id": preorder_sale_id}
    ).mappings().first()
    if row is None:
        raise PreorderSaleNotFoundError(preorder_sale_id)
    return _row_to_result(row)


def list_preorder_sales(
    conn: Connection, status: Optional[str] = None, sku: Optional[str] = None
) -> list[PreorderSaleResult]:
    """Every pre-order sale, optionally narrowed by status and/or item —
    newest-recorded-last (stable id order), mirroring this project's other
    list_* read helpers (``inventory/queries.py``).
    """
    rows = conn.execute(
        text(
            _SELECT_PREORDER_SALE
            + " WHERE (:status IS NULL OR ps.status = :status) "
            + "AND (:sku IS NULL OR UPPER(i.sku) = UPPER(:sku)) "
            + "ORDER BY ps.id"
        ),
        {"status": status, "sku": sku},
    ).mappings().all()
    return [_row_to_result(r) for r in rows]


# --------------------------------------------------------------------- #
# Record a new pending pre-order sale
# --------------------------------------------------------------------- #


def record_preorder_sale(
    conn: Connection,
    sku: str,
    quantity: int,
    sale_date: Optional[date_type] = None,
    reference: Optional[str] = None,
) -> PreorderSaleResult:
    """Records a new PENDING pre-order sale. Rejects a non-fungible item
    with a clear, application-level ``ValidationError`` — the real DB-level
    backstop for this same check is
    ``check_preorder_sale_item_is_fungible()`` (migrations/
    005_add_preorder_sales.sql), same two-layer discipline as every other
    cross-table invariant in this project.
    """
    if quantity is None or not isinstance(quantity, int) or quantity <= 0:
        raise ValidationError("Pre-order sale quantity must be a positive integer.")

    item = get_item_by_sku(conn, sku)
    if item is None:
        raise ValidationError(f"No item with SKU {sku!r}.")
    if item.identity_mode != "fungible":
        raise ValidationError(
            f"Item {sku!r} is identity_mode={item.identity_mode!r} — pre-order sales are only "
            "supported for fungible items in this milestone."
        )

    sale_date = sale_date or date_type.today()
    row_id = conn.execute(
        text(
            """
            INSERT INTO preorder_sales (item_id, quantity, sale_date, reference)
            VALUES (:item_id, :quantity, :sale_date, :reference)
            RETURNING id
            """
        ),
        {"item_id": item.id, "quantity": quantity, "sale_date": sale_date, "reference": reference},
    ).scalar_one()
    return get_preorder_sale(conn, row_id)


# --------------------------------------------------------------------- #
# Cancel a still-pending pre-order sale
# --------------------------------------------------------------------- #


def cancel_preorder_sale(conn: Connection, preorder_sale_id: int) -> PreorderSaleResult:
    """Cancels a still-PENDING pre-order sale via a plain atomic
    ``UPDATE ... WHERE status = 'pending'`` compare-and-swap (see module
    docstring — never a trigger-based ``SELECT ... FOR UPDATE``). If the
    row is already ``fulfilled``/``cancelled`` (including the case where a
    concurrent fulfillment/cancellation won the race a moment earlier),
    raises ``PreorderSaleNotPendingError`` rather than silently no-op'ing —
    nothing was changed.
    """
    gated = conn.execute(
        text(
            """
            UPDATE preorder_sales
            SET status = 'cancelled', cancelled_at = now()
            WHERE id = :id AND status = 'pending'
            RETURNING id
            """
        ),
        {"id": preorder_sale_id},
    ).first()

    if gated is None:
        # Best-effort, race-tolerant diagnostic lookup ONLY — same pattern
        # as inventory/depletions.py::_diagnose_serial_depletion_failure.
        # The real gate already ran above; this just produces a specific,
        # actionable error message (not found vs. already fulfilled vs.
        # already cancelled).
        existing = conn.execute(
            text("SELECT status FROM preorder_sales WHERE id = :id"), {"id": preorder_sale_id}
        ).first()
        if existing is None:
            raise PreorderSaleNotFoundError(preorder_sale_id)
        raise PreorderSaleNotPendingError(preorder_sale_id, current_status=existing[0])

    return get_preorder_sale(conn, preorder_sale_id)


# --------------------------------------------------------------------- #
# Fulfillment orchestration
# --------------------------------------------------------------------- #


@dataclass
class FulfilledLine:
    preorder_sale_id: int
    depletion: FungibleDepletionResult


@dataclass
class FulfillmentResult:
    purchase_id: int
    sku: str
    purchase: Optional[SavedPurchase]  # None when fulfilling via an existing purchase_id
    fulfilled: list[FulfilledLine] = field(default_factory=list)


def fulfill_preorder_sales(
    conn: Connection,
    preorder_sale_ids: list[int],
    purchase: Optional[PurchaseInput] = None,
    purchase_id: Optional[int] = None,
) -> FulfillmentResult:
    """Fulfills one or more PENDING pre-order sales — all for the SAME
    item — from a purchase, either a brand-new one (``purchase``, saved
    here via the real, unmodified ``inventory.purchases.save_purchase()``)
    or a purchase that was already saved earlier (``purchase_id``,
    referenced but not re-created). Exactly one of the two must be given.

    Orchestration, in order:
      1. Resolve the purchase (save a new one, or look up the item(s) an
         existing one covers) — never reimplements ``save_purchase()``'s
         own validation/allocation logic.
      2. Validate every given pre-order sale id exists, is currently
         PENDING, and all reference the SAME item — which must also be one
         of the resolved purchase's own line items (``PreorderItemMismatchError``
         otherwise). This is a fast, clear pre-check; see step 3 for the
         real, authoritative gate.
      3. For each pre-order sale (in id order): an atomic
         ``pending -> fulfilled`` compare-and-swap (the SAME real gate
         ``cancel_preorder_sale`` uses — see module docstring), then one
         real ``deplete_fungible()`` call tagged with
         ``preorder_sale_id=...``.

    This whole call happens inside the CALLER's transaction (this module
    never commits/rolls back itself — same convention as
    ``inventory/purchases.py``/``inventory/depletions.py``). If ANY step
    fails partway (a pre-order sale was already claimed concurrently, or
    the purchase didn't actually bring enough stock to cover everything
    requested), the exception propagates all the way up uncaught — nothing
    from this call was committed, so a genuinely brand-new purchase created
    in step 1 is cleanly rolled back too, rather than being left half-
    fulfilled. This is deliberate: partially fulfilling "the batch the user
    just selected together" is not a sensible partial-success state, unlike
    Milestone 6's per-row-independent ``process_confirmed_rows`` (which
    processes an unrelated batch of rows, not one linked purchase).
    """
    if not preorder_sale_ids:
        raise ValidationError("At least one pre-order sale id must be given to fulfill.")
    if len(set(preorder_sale_ids)) != len(preorder_sale_ids):
        raise ValidationError("The same pre-order sale id was given more than once.")
    if (purchase is None) == (purchase_id is None):
        raise ValidationError("Exactly one of purchase / purchase_id must be given.")

    saved_purchase: Optional[SavedPurchase] = None
    if purchase is not None:
        saved_purchase = save_purchase(conn, purchase)
        resolved_purchase_id = saved_purchase.id
        purchased_item_ids = {line.item_id for line in saved_purchase.lines}
    else:
        resolved_purchase_id = purchase_id
        rows = conn.execute(
            text("SELECT DISTINCT item_id FROM purchase_line_items WHERE purchase_id = :pid"),
            {"pid": purchase_id},
        ).all()
        if not rows:
            raise ValidationError(f"No purchase with id {purchase_id!r} (or it has no line items).")
        purchased_item_ids = {r[0] for r in rows}

    rows = conn.execute(
        text(_SELECT_PREORDER_SALE + " WHERE ps.id = ANY(:ids)"),
        {"ids": list(preorder_sale_ids)},
    ).mappings().all()
    found_by_id = {r["id"]: r for r in rows}
    missing = [pid for pid in preorder_sale_ids if pid not in found_by_id]
    if missing:
        raise PreorderSaleNotFoundError(missing[0])

    item_ids_involved = {r["item_id"] for r in rows}
    if len(item_ids_involved) > 1:
        raise PreorderItemMismatchError(
            "All pre-order sales being fulfilled together must be for the same item."
        )
    (item_id,) = item_ids_involved
    if item_id not in purchased_item_ids:
        sku = rows[0]["sku"]
        raise PreorderItemMismatchError(
            f"The supplied purchase has no line item for {sku!r} — it cannot fulfill "
            "pre-order sale(s) for that item."
        )

    for pid in preorder_sale_ids:
        row = found_by_id[pid]
        if row["status"] != "pending":
            raise PreorderSaleNotPendingError(pid, current_status=row["status"])

    fulfilled: list[FulfilledLine] = []
    for pid in preorder_sale_ids:
        row = found_by_id[pid]
        gated = conn.execute(
            text(
                """
                UPDATE preorder_sales
                SET status = 'fulfilled', fulfilled_at = now()
                WHERE id = :id AND status = 'pending'
                RETURNING id
                """
            ),
            {"id": pid},
        ).first()
        if gated is None:
            # A concurrent cancel/fulfill won the race between our
            # pre-check above and this real gate — the whole fulfillment
            # aborts (see docstring: no partial fulfillment of one linked
            # batch), and nothing from this call was ever committed.
            raise PreorderSaleNotPendingError(pid)

        reference = f"Pre-order fulfillment (pre-order sale #{pid})"
        if row["reference"]:
            reference += f" — {row['reference']}"

        depletion = deplete_fungible(
            conn,
            sku=row["sku"],
            quantity=row["quantity"],
            reference=reference,
            preorder_sale_id=pid,
        )
        fulfilled.append(FulfilledLine(preorder_sale_id=pid, depletion=depletion))

    return FulfillmentResult(
        purchase_id=resolved_purchase_id,
        sku=rows[0]["sku"],
        purchase=saved_purchase,
        fulfilled=fulfilled,
    )
