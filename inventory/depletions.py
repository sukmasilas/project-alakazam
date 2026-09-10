"""Sale-side depletion — Milestone 5. Turns Alakazam from acquisition-only
into real in/out inventory movement: ``deplete_fungible`` and
``deplete_serial_unit`` are the two entry points, mirroring the identity-
mode split already fixed in Milestone 2 (``inventory/purchases.py``,
``inventory/items.py``).

No sale price or revenue is captured anywhere in this module — only
quantity and cost-basis movement. See CLAUDE.md's Milestone 5 decision and
``docs/design/milestone-5-depletion-design.md`` for the full rationale.

Both functions never commit/roll back the caller's connection themselves —
same convention as ``inventory/purchases.py``/``inventory/items.py``; the
caller (a script, a test, or ``webapp/dbdep.py::get_write_conn``) owns the
transaction.

## The negative-stock invariant — two layers, same pattern as Milestone 2

1. **Fast, app-level pre-check** (this module): read current on-hand stats
   fresh via ``inventory.queries.get_item_stats`` (which, as of this
   milestone, already nets purchased against depleted) and reject before
   ever touching the database if the request is too large. Fast, clear
   error, no row written.
2. **Real DB-level backstop**:
   - Fungible: ``fungible_depletions`` has an AFTER INSERT trigger
     (``check_fungible_depletion_not_negative()``,
     ``migrations/003_add_depletions.sql``) that acquires a
     transaction-scoped Postgres advisory lock keyed on the item being
     depleted (``pg_advisory_xact_lock(item_id)``) before re-summing
     purchased vs. depleted — the same real backstop Milestone 2's
     reconciliation trigger and SKU/serial uniqueness triggers provide,
     extended with an explicit lock because THIS invariant specifically
     needs to survive two genuinely concurrent transactions racing on the
     same item, not just a single-transaction multi-statement sequence.

     **QA-found bug, fixed 2026-09-10**: this trigger originally used
     ``SELECT ... FOR UPDATE`` on the parent ``items`` row instead of an
     advisory lock. That pattern deadlocks under genuine (not just
     event-staggered) concurrency — see the trigger's own comment in
     ``migrations/003_add_depletions.sql`` for the full root-cause
     analysis (a lock-upgrade conflict against the row's own
     foreign-key-driven ``FOR KEY SHARE`` lock). The advisory lock fix
     eliminates the deadlock structurally: each call acquires exactly one
     lock for its whole duration, and a single-lock-per-transaction
     protocol cannot deadlock against another instance of itself.

     The trigger raises with ``ERRCODE = '23514'`` so it surfaces here as
     a real ``sqlalchemy.exc.IntegrityError`` — caught below via a
     ``conn.begin_nested()`` savepoint, exactly the same pattern
     ``inventory/purchases.py`` uses for its own DB-level uniqueness
     backstop, so a genuine race is exactly as safe for a caller to catch
     and recover from as the far more common sequential over-depletion
     case. As defense-in-depth (belt-and-suspenders, since the advisory
     lock is expected to make this unreachable in normal operation), a
     real Postgres deadlock (``sqlalchemy.exc.OperationalError``,
     ``SQLSTATE 40P01``) is ALSO caught below and translated into a
     clearly-labeled ``DepletionConflictError`` — never silently
     reported as ``InsufficientStockError``, and never left to propagate
     as a raw, uncaught 500.
   - Serialized: no trigger needed — ``deplete_serial_unit`` performs the
     state change as ONE atomic
     ``UPDATE serial_units SET status = 'sold', ... WHERE status = 'on_hand'
     AND ...``. Postgres's own row-level MVCC locking on that statement is
     already a genuine concurrency backstop: two concurrent UPDATEs
     targeting the same row serialize on the row itself, so two
     "sell the last unit" requests can never both succeed (the loser's
     WHERE clause matches zero rows once it re-evaluates after the winner
     commits). QA independently verified this path never deadlocks under
     a genuinely-simultaneous (barrier-synchronized) race, across 8/8
     trials, in contrast to the fungible path's pre-fix behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, OperationalError

from inventory.allocation import round_half_up
from inventory.exceptions import (
    AlreadyDepletedError,
    DepletionConflictError,
    InsufficientStockError,
    ItemMismatchError,
    SerialUnitNotFoundError,
    ValidationError,
)
from inventory.queries import get_item_by_sku, get_item_stats

# The Postgres SQLSTATE for a genuine deadlock (deadlock_detected). See
# module docstring's "QA-found bug" note and the trigger's own comment in
# migrations/003_add_depletions.sql for the full story — the advisory-lock
# fix there is expected to make this unreachable, but it's still checked
# for explicitly here rather than assumed away, since silently letting a
# deadlock propagate as a raw 500 is exactly the bug being fixed.
_POSTGRES_DEADLOCK_SQLSTATE = "40P01"


@dataclass
class FungibleDepletionResult:
    id: int
    item_id: int
    sku: str
    quantity: int
    unit_cost: Decimal
    total_cost: int
    depletion_date: date_type
    reference: Optional[str]


@dataclass
class SerialDepletionResult:
    id: int
    item_id: int
    sku: str
    serial_id: str
    acquired_cost: Decimal
    sold_date: date_type
    reference: Optional[str]


def deplete_fungible(
    conn: Connection,
    sku: str,
    quantity: int,
    depletion_date: Optional[date_type] = None,
    reference: Optional[str] = None,
) -> FungibleDepletionResult:
    """Depletes ``quantity`` units of a fungible item's on-hand stock at
    the current moving weighted-average cost (on-hand cost basis / on-hand
    quantity, computed fresh here — see module docstring's Business model
    reference and CLAUDE.md's Milestone 5 decision: this is NOT FIFO batch
    order). Rejects (``InsufficientStockError``) if ``quantity`` exceeds
    current on-hand quantity.
    """
    if quantity is None or not isinstance(quantity, int) or quantity <= 0:
        raise ValidationError("Depletion quantity must be a positive integer.")

    item = get_item_by_sku(conn, sku)
    if item is None:
        raise ValidationError(f"No item with SKU {sku!r}.")
    if item.identity_mode != "fungible":
        raise ValidationError(
            f"Item {sku!r} is identity_mode={item.identity_mode!r} — "
            "use deplete_serial_unit() for a serialized item."
        )

    stats = get_item_stats(conn, sku)
    if quantity > stats.quantity:
        raise InsufficientStockError(sku=item.sku, requested=quantity, available=stats.quantity)

    # stats.quantity >= quantity >= 1 here, so this division is always safe
    # — an on-hand quantity of 0 would already have failed the check above
    # for any positive request.
    unit_cost = stats.cost_basis / stats.quantity
    total_cost = round_half_up(unit_cost * quantity)
    depletion_date = depletion_date or date_type.today()

    try:
        # Savepoint, not a plain execute — see module docstring: under
        # genuine concurrency the pre-check above can be stale by the time
        # this INSERT actually runs, and the real trigger backstop is what
        # catches that case. Scoping the abort to a savepoint keeps the
        # resulting IntegrityError safe for a caller to catch without
        # losing the rest of the transaction (same reasoning as
        # inventory/purchases.py's new-item/serial inserts).
        with conn.begin_nested():
            row_id = conn.execute(
                text(
                    """
                    INSERT INTO fungible_depletions
                        (item_id, quantity, unit_cost, total_cost, depletion_date, reference)
                    VALUES (:item_id, :quantity, :unit_cost, :total_cost, :depletion_date, :reference)
                    RETURNING id
                    """
                ),
                {
                    "item_id": item.id,
                    "quantity": quantity,
                    "unit_cost": unit_cost,
                    "total_cost": total_cost,
                    "depletion_date": depletion_date,
                    "reference": reference,
                },
            ).scalar_one()
    except IntegrityError as exc:
        raise InsufficientStockError(sku=item.sku, requested=quantity, available=None) from exc
    except OperationalError as exc:
        pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
        if pgcode == _POSTGRES_DEADLOCK_SQLSTATE:
            raise DepletionConflictError(sku=item.sku) from exc
        raise  # pragma: no cover — an unrelated OperationalError is a real, unexpected failure

    return FungibleDepletionResult(
        id=row_id,
        item_id=item.id,
        sku=item.sku,
        quantity=quantity,
        unit_cost=unit_cost,
        total_cost=total_cost,
        depletion_date=depletion_date,
        reference=reference,
    )


def _diagnose_serial_depletion_failure(
    conn: Connection, normalized_serial_id: str, expected_sku: Optional[str]
) -> None:
    """Called only after the real atomic UPDATE (see deplete_serial_unit)
    already affected zero rows — this is a read-only, best-effort lookup
    purely to produce a SPECIFIC, actionable error message (not found vs.
    already sold vs. wrong item). It is diagnostic only, never the actual
    gate: a harmless race is possible here (another concurrent depletion
    could change the row between the UPDATE and this SELECT), but that's
    fine — either way this function's caller already knows zero rows were
    affected by its own UPDATE, so nothing was incorrectly depleted
    regardless of what this diagnostic reports.
    """
    existing = conn.execute(
        text(
            """
            SELECT su.status, i.sku
            FROM serial_units su
            JOIN items i ON i.id = su.item_id
            WHERE UPPER(su.serial_id) = UPPER(:serial_id)
            """
        ),
        {"serial_id": normalized_serial_id},
    ).mappings().first()

    if existing is None:
        raise SerialUnitNotFoundError(normalized_serial_id)
    if expected_sku is not None and existing["sku"].upper() != expected_sku.strip().upper():
        raise ItemMismatchError(normalized_serial_id, expected_sku, existing["sku"])
    if existing["status"] != "on_hand":
        raise AlreadyDepletedError(normalized_serial_id)
    # Every condition the UPDATE's WHERE clause checks was satisfied here,
    # yet the UPDATE still affected zero rows — would mean a real bug in
    # this function's own SQL, not a legitimate rejection. Fail loudly
    # rather than silently reporting a wrong reason.
    raise ValidationError(
        f"Could not deplete serial/asset unit {normalized_serial_id!r} for an unknown reason."
    )


def deplete_serial_unit(
    conn: Connection,
    serial_id: str,
    expected_sku: Optional[str] = None,
    sold_date: Optional[date_type] = None,
    reference: Optional[str] = None,
) -> SerialDepletionResult:
    """Marks one specific serialized unit as sold. Rejects
    (``SerialUnitNotFoundError`` / ``AlreadyDepletedError`` /
    ``ItemMismatchError``) if the unit doesn't exist, isn't currently
    ``on_hand``, or (when ``expected_sku`` is given) belongs to a different
    item than claimed. The unit's own ``acquired_cost`` is the real cost
    removed — no weighted-average computation needed (see module
    docstring).

    ``expected_sku``, when given, is enforced as part of the SAME atomic
    UPDATE statement below (not a separate check-then-update) — see that
    statement's WHERE clause.
    """
    normalized = (serial_id or "").strip()
    if not normalized:
        raise ValidationError("serial_id is required.")

    sold_date = sold_date or date_type.today()

    row = conn.execute(
        text(
            """
            UPDATE serial_units su
            SET status = 'sold', sold_date = :sold_date, sold_reference = :reference
            FROM items i
            WHERE su.item_id = i.id
              AND UPPER(su.serial_id) = UPPER(:serial_id)
              AND su.status = 'on_hand'
              AND (:expected_sku IS NULL OR UPPER(i.sku) = UPPER(:expected_sku))
            RETURNING su.id, su.serial_id, su.item_id, su.acquired_cost, i.sku
            """
        ),
        {
            "sold_date": sold_date,
            "reference": reference,
            "serial_id": normalized,
            "expected_sku": expected_sku,
        },
    ).mappings().first()

    if row is None:
        _diagnose_serial_depletion_failure(conn, normalized, expected_sku)
        # _diagnose_serial_depletion_failure always raises — this is
        # unreachable, kept only so a type-checker/reader can see this
        # function never falls through without returning or raising.
        raise AssertionError("unreachable")

    return SerialDepletionResult(
        id=row["id"],
        item_id=row["item_id"],
        sku=row["sku"],
        serial_id=row["serial_id"],
        acquired_cost=Decimal(row["acquired_cost"]),
        sold_date=sold_date,
        reference=reference,
    )
