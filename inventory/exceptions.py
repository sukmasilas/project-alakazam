"""Shared exception types raised by the business logic in this package.

Kept as a real, specific hierarchy (never a bare ValueError) so callers —
and tests — can distinguish "this purchase doesn't reconcile" from "this
SKU/serial already exists" from "the input is structurally incomplete".
"""
from __future__ import annotations

from typing import Optional


class AlakazamError(Exception):
    """Base class for every business-logic error in this package."""


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
