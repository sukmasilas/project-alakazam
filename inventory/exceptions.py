"""Shared exception types raised by the business logic in this package.

Kept as a real, specific hierarchy (never a bare ValueError) so callers —
and tests — can distinguish "this purchase doesn't reconcile" from "this
SKU/serial already exists" from "the input is structurally incomplete".
"""
from __future__ import annotations


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
