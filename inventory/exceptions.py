"""Shared exception types raised by the business logic in this package.

Kept as a real, specific hierarchy (never a bare ValueError) so callers —
and tests — can distinguish "this purchase doesn't reconcile" from "this
SKU/serial already exists" from "the input is structurally incomplete".
"""
from __future__ import annotations

from typing import Optional


class AlakazamError(Exception):
    """Base class for every business-logic error in this package."""


class PurchaseRefConflictError(AlakazamError):
    """Raised only if ``save_purchase()`` exhausts every retry attempt at
    generating a unique ``purchase_ref`` under genuine, sustained
    concurrent contention (see that function's own docstring/comment for
    the full root-cause analysis — two concurrent transactions racing on
    ``_generate_purchase_ref()``'s plain, unlocked ``SELECT`` + Python
    ``max()``). In normal operation this is expected to be effectively
    unreachable: the bounded retry loop already catches and recovers from
    the far more common case of losing exactly one such race. Kept as a
    real, distinct, clearly-labeled exception (never left to propagate as
    a raw, uncaught ``IntegrityError``/500) so a caller can tell "please
    just retry the whole request" apart from every other kind of failure
    this function can raise.
    """

    def __init__(self, attempts: int):
        self.attempts = attempts
        super().__init__(
            f"Could not generate a unique purchase reference after {attempts} attempt(s) — "
            "please try saving this purchase again."
        )


class ReconciliationError(AlakazamError):
    """Raised when a purchase's line items don't sum exactly (to the
    rupiah) to its Total Amount Paid. This is the "debits = credits"
    equivalent for this system — see CLAUDE.md's brief and
    docs/design/ui-ux-design.md's balance-check indicator.
    """

    def __init__(self, running_total: int, total_amount_paid: int):
        self.running_total = running_total
        self.total_amount_paid = total_amount_paid
        self.diff = total_amount_paid - running_total
        super().__init__(
            f"Purchase is out of balance by {self.diff:+d}: "
            f"line items sum to {running_total}, Total Amount Paid is {total_amount_paid}."
        )


class DuplicateSkuError(AlakazamError):
    """Raised when a candidate SKU already exists — either in the database,
    or against another line being created in the same purchase.
    """

    def __init__(self, sku: str):
        self.sku = sku
        super().__init__(f"SKU {sku!r} already exists.")


class DuplicateSerialError(AlakazamError):
    """Raised when a candidate serial/asset ID already exists — either in
    the database, or against another unit being created in the same
    purchase.
    """

    def __init__(self, serial_id: str):
        self.serial_id = serial_id
        super().__init__(f"Serial/Asset ID {serial_id!r} already exists.")


class ValidationError(AlakazamError):
    """Raised for structurally incomplete or internally inconsistent input
    (e.g. a serialized line missing unit assignments, a new-item line
    missing a category, per-unit serial costs that don't sum to the line's
    total).
    """


class InsufficientStockError(AlakazamError):
    """Raised when a fungible depletion request exceeds current on-hand
    quantity for that SKU — the core negative-stock invariant from
    CLAUDE.md's Milestone 5 brief. ``available`` is the on-hand quantity
    the pre-check in inventory/depletions.py actually saw; it's left as
    ``None`` when this is instead raised from the real DB-level trigger
    catching a genuine concurrent race (see
    check_fungible_depletion_not_negative() in
    migrations/003_add_depletions.sql), since by the time that fires the
    caller's own pre-check value is already stale and not worth reporting
    as if it were current.
    """

    def __init__(self, sku: str, requested: int, available: Optional[int]):
        self.sku = sku
        self.requested = requested
        self.available = available
        if available is None:
            message = (
                f"Cannot deplete {requested} unit(s) of {sku!r} — on-hand stock changed "
                "concurrently and is no longer sufficient."
            )
        else:
            message = (
                f"Cannot deplete {requested} unit(s) of {sku!r} — only {available} on hand."
            )
        super().__init__(message)


class DepletionConflictError(AlakazamError):
    """Defensive safety net (QA-found bug, fixed 2026-09-10): raised if a
    fungible depletion ever hits a real Postgres deadlock
    (``SQLSTATE 40P01``) despite the structural fix in
    ``check_fungible_depletion_not_negative()`` (migrations/
    003_add_depletions.sql) that's designed to make this unreachable — see
    that trigger's own comment for the full root-cause analysis (a
    lock-upgrade deadlock caused by ``SELECT ... FOR UPDATE`` racing
    against the implicit foreign-key lock every insert already takes,
    fixed by switching to a transaction-scoped advisory lock, which cannot
    deadlock against another instance of itself). Kept as a distinct,
    clearly-labeled exception (never silently reported as
    ``InsufficientStockError``, and never left to propagate as a raw,
    uncaught 500) in case some future change to this locking pattern ever
    reintroduces a genuine deadlock. The caller's own request was NOT
    honored — nothing was written — and simply retrying is expected to
    succeed.
    """

    def __init__(self, sku: str):
        self.sku = sku
        super().__init__(
            f"A transient database conflict occurred while depleting {sku!r} — please try again."
        )


class SerialUnitNotFoundError(AlakazamError):
    """Raised when a depletion targets a serial/asset ID that doesn't exist
    at all.
    """

    def __init__(self, serial_id: str):
        self.serial_id = serial_id
        super().__init__(f"No serial/asset unit with ID {serial_id!r}.")


class AlreadyDepletedError(AlakazamError):
    """Raised when a depletion targets a serial/asset unit that isn't
    currently on_hand (already sold).
    """

    def __init__(self, serial_id: str):
        self.serial_id = serial_id
        super().__init__(f"Serial/Asset unit {serial_id!r} is not on hand (already sold).")


class DuplicateEbaySaleError(AlakazamError):
    """Raised when a depletion is posted with an ``ebay_transaction_id``
    that already exists on another ``fungible_depletions``/``serial_units``
    row (Milestone 6's real DB-level uniqueness backstop — see
    migrations/004_add_ebay_sales_import.sql). In normal operation this is
    expected to be unreachable: ``ingestion/ebay_import.py``'s own
    partial-unique-index-backed import step already prevents the same
    eBay Transaction ID from ever being stored as a second reviewable row,
    and ``process_confirmed_rows()`` only ever calls the depletion engine
    once per row. Kept as a real, distinct exception anyway (never silently
    reported as ``InsufficientStockError``/``AlreadyDepletedError``) so a
    genuine second layer of protection exists even if some future code path
    ever calls the depletion engine directly with a duplicate ID, bypassing
    the review-queue layer entirely.
    """

    def __init__(self, ebay_transaction_id: Optional[str]):
        self.ebay_transaction_id = ebay_transaction_id
        super().__init__(
            f"eBay transaction {ebay_transaction_id!r} has already been posted to inventory — "
            "refusing to deplete it a second time."
        )


class DuplicatePreorderFulfillmentError(AlakazamError):
    """Raised when a fungible depletion is posted with a
    ``preorder_sale_id`` that already has a depletion recorded against it
    (Milestone 7's real DB-level uniqueness backstop — see
    migrations/005_add_preorder_sales.sql). In normal operation this is
    expected to be unreachable: ``inventory.preorders.fulfill_preorder_sales``
    already gates on the pre-order sale's own ``status`` via an atomic
    ``pending`` -> ``fulfilled`` compare-and-swap before ever calling the
    depletion engine, and never calls it twice for the same pre-order sale.
    Kept as a real, distinct exception anyway (never silently reported as
    ``InsufficientStockError``) so a genuine second layer of protection
    exists even if some future code path ever calls the depletion engine
    directly with a duplicate id.
    """

    def __init__(self, preorder_sale_id):
        self.preorder_sale_id = preorder_sale_id
        super().__init__(
            f"Pre-order sale {preorder_sale_id!r} already has a depletion posted against it — "
            "refusing to deplete it a second time."
        )


class ItemMismatchError(AlakazamError):
    """Raised when a depletion claims a serial/asset unit belongs to a
    given SKU, but it's actually catalogued under a different item.
    """

    def __init__(self, serial_id: str, expected_sku: str, actual_sku: str):
        self.serial_id = serial_id
        self.expected_sku = expected_sku
        self.actual_sku = actual_sku
        super().__init__(
            f"Serial/Asset unit {serial_id!r} belongs to item {actual_sku!r}, not {expected_sku!r}."
        )


class PreorderSaleNotFoundError(AlakazamError):
    """Raised when a pre-order sale id doesn't exist at all (Milestone 7:
    ``inventory/preorders.py``).
    """

    def __init__(self, preorder_sale_id):
        self.preorder_sale_id = preorder_sale_id
        super().__init__(f"No pre-order sale with id {preorder_sale_id!r}.")


class PreorderSaleNotPendingError(AlakazamError):
    """Raised when an action that requires a pre-order sale to still be
    ``pending`` (cancel, fulfill) targets one that's already ``fulfilled``
    or ``cancelled`` — including the case where this is discovered only at
    the real atomic compare-and-swap UPDATE (a concurrent cancel/fulfill
    won the race a moment earlier), not just an application-level
    pre-check. Both 'fulfilled' and 'cancelled' are terminal states in this
    project — see CLAUDE.md's "no correction/reversal flow yet" policy.
    """

    def __init__(self, preorder_sale_id, current_status: Optional[str] = None):
        self.preorder_sale_id = preorder_sale_id
        self.current_status = current_status
        if current_status:
            message = (
                f"Pre-order sale {preorder_sale_id} is already {current_status!r} — "
                "only a still-pending pre-order sale can be cancelled or fulfilled."
            )
        else:
            message = (
                f"Pre-order sale {preorder_sale_id} is no longer pending — it may have "
                "already been fulfilled or cancelled concurrently."
            )
        super().__init__(message)


class PreorderItemMismatchError(AlakazamError):
    """Raised when a fulfillment orchestration is asked to fulfill pre-order
    sales that don't all reference the same item, or whose item doesn't
    match any line item on the purchase supplied to fulfill them (Milestone
    7's ``inventory.preorders.fulfill_preorder_sales``).
    """

    def __init__(self, message: str):
        super().__init__(message)
