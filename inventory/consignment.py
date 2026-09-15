"""Consignment tracking — Milestone 8.

Items owned by a third-party consignor, physically held by the seller,
never the seller's own asset. See CLAUDE.md's "Milestone 8 decisions,
confirmed 2026-09-15" for the full scope and rationale. Summary:

- Serialized only — no fungible consignment support in this milestone. A
  consigned unit's own direct cost is genuinely Rp 0 (the seller never
  bought it) — no weighted-average pooling concern at all.
- Intake is NOT a Purchase — no money changes hands, ``save_purchase()`` is
  never involved. ``intake_consigned_units()`` below is the real engine for
  this, reusing the exact same SKU/serial generation and uniqueness
  primitives ``inventory/purchases.py``/``inventory/items.py`` already use
  (never reimplemented here).
- Selling a consigned unit reuses the existing, proven
  ``inventory.depletions.deplete_serial_unit()`` for the real inventory-
  movement half (marking the unit sold, correctly removing $0 cost — that's
  correct, not a bug), wrapped here in ``sell_consigned_unit()``, which also
  creates a reimbursement record (defaulting to ``unpaid``) in the SAME
  transaction — mirrors how ``inventory/preorders.py::fulfill_preorder_sales``
  wraps the existing depletion/purchase engines rather than reinventing
  inventory movement.
- A paid/unpaid reimbursement STATUS is tracked per sold consigned unit — a
  real scope addition beyond pure inventory tracking, but still never an
  amount. Payout amount computation stays entirely on Project-Noctrowl's
  side (two live formulas, decided manually per-sale there) — same
  "no sale price/revenue anywhere in Alakazam" boundary as every prior
  milestone.
- The unpaid -> paid status transition (``mark_reimbursement_paid``) uses
  the same plain atomic ``UPDATE ... WHERE status = 'unpaid'`` compare-and-
  swap pattern already proven safe in Milestones 5-7 — never a trigger-based
  ``SELECT ... FOR UPDATE``. ``paid`` is TERMINAL — same "no correction/
  reversal flow yet" policy already carried through every prior milestone.

## SKU convention for consigned items (placeholder — see module note below)

``CONSIGN-{CATEGORY_PREFIX}-{first 1-2 words of name, slugified}-{4-digit
sequence}`` — e.g. "Rolex Daytona" consigned under Watches ->
``CONSIGN-WATCH-ROLEX-DAYTONA-0001``. Built by reusing
``inventory.sku.generate_sku()`` UNCHANGED: this module simply passes
``f"CONSIGN-{category.sku_prefix}"`` as the "category prefix" argument,
which that function treats as an opaque leading string — no change to
``inventory/sku.py`` needed. Same "placeholder pending real user
confirmation, not a settled rule" treatment CLAUDE.md already gives the
original SKU/serial-ID conventions from Milestone 1.

## Two-layer discipline, consistent with every prior milestone

Both the fungible-only-for-consignment/item-separation invariants and the
purchase-traceability consistency invariant have a real DB-level backstop
(migrations/006_add_consignment.sql's two triggers) in addition to the
application-level checks below — never just an app-level check alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from inventory.depletions import SerialDepletionResult, deplete_serial_unit
from inventory.exceptions import (
    ConsignorNotFoundError,
    DuplicateSerialError,
    DuplicateSkuError,
    ItemNotConsignedError,
    ReimbursementAlreadyExistsError,
    ReimbursementNotFoundError,
    ReimbursementNotUnpaidError,
    ValidationError,
)
from inventory.queries import (
    count_serial_units_for_item,
    get_category_by_code,
    get_item_by_sku,
    get_skus_for_prefix_slug,
)
from inventory.serials import generate_serials_for_line
from inventory.sku import generate_sku, slugify_name
from inventory.uniqueness import find_duplicate_keys, serial_exists, sku_exists

CONSIGN_SKU_PREFIX_TAG = "CONSIGN"


# --------------------------------------------------------------------- #
# Consignors — simple CRUD (CLAUDE.md: "not meant to be a full CRM").
# --------------------------------------------------------------------- #


@dataclass
class Consignor:
    id: int
    name: str
    contact_info: Optional[str]


def create_consignor(conn: Connection, name: str, contact_info: Optional[str] = None) -> Consignor:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Consignor name is required.")
    contact_info = (contact_info or "").strip() or None
    row_id = conn.execute(
        text("INSERT INTO consignors (name, contact_info) VALUES (:name, :contact_info) RETURNING id"),
        {"name": name, "contact_info": contact_info},
    ).scalar_one()
    return Consignor(id=row_id, name=name, contact_info=contact_info)


def list_consignors(conn: Connection) -> list[Consignor]:
    rows = conn.execute(
        text("SELECT id, name, contact_info FROM consignors ORDER BY name")
    ).mappings().all()
    return [Consignor(**row) for row in rows]


def get_consignor(conn: Connection, consignor_id: int) -> Optional[Consignor]:
    row = conn.execute(
        text("SELECT id, name, contact_info FROM consignors WHERE id = :id"),
        {"id": consignor_id},
    ).mappings().first()
    return Consignor(**row) if row else None


# --------------------------------------------------------------------- #
# Intake — creating/reusing a consigned item and adding zero-cost serial
# units. NOT a Purchase — save_purchase() is never called here.
# --------------------------------------------------------------------- #


@dataclass
class ConsignmentIntakeInput:
    consignor_id: int

    # Exactly one of the two below must be given.
    sku: Optional[str] = None  # reuse an EXISTING consigned item
    new_item_name: Optional[str] = None  # OR create a brand-new consigned item
    new_item_category_code: Optional[str] = None  # required if new_item_name is given
    new_item_sku: Optional[str] = None  # optional explicit SKU override for a new item

    quantity: int = 1
    # Optional explicit serial IDs (length must == quantity if given) / photo
    # references — same "give it, or let it auto-generate/default" pattern
    # as inventory/purchases.py's SerialUnitInput.
    serial_ids: Optional[list[str]] = None
    photo_references: Optional[list[str]] = None


@dataclass
class ConsignmentIntakeResult:
    item_id: int
    sku: str
    item_name: str
    consignor_id: int
    serial_ids: list[str] = field(default_factory=list)


def intake_consigned_units(conn: Connection, intake: ConsignmentIntakeInput) -> ConsignmentIntakeResult:
    if (intake.sku is None) == (intake.new_item_name is None):
        raise ValidationError("Exactly one of sku / new_item_name must be given.")
    if intake.quantity is None or not isinstance(intake.quantity, int) or intake.quantity <= 0:
        raise ValidationError("Intake quantity must be a positive integer.")

    consignor = get_consignor(conn, intake.consignor_id)
    if consignor is None:
        raise ConsignorNotFoundError(intake.consignor_id)

    if intake.sku is not None:
        item = get_item_by_sku(conn, intake.sku)
        if item is None:
            raise ValidationError(f"No item with SKU {intake.sku!r}.")
        if item.identity_mode != "serialized":
            raise ValidationError(
                f"Item {item.sku!r} is identity_mode={item.identity_mode!r} — consignment intake "
                "only supports serialized items (no fungible consignment in this milestone)."
            )
        if item.consignor_id is None:
            raise ItemNotConsignedError(item.sku)
        if item.consignor_id != intake.consignor_id:
            raise ValidationError(
                f"Item {item.sku!r} is already tagged to a different consignor "
                f"(id={item.consignor_id}), not consignor id={intake.consignor_id}."
            )
        item_id = item.id
        sku = item.sku
        item_name = item.name
    else:
        name = (intake.new_item_name or "").strip()
        if not name:
            raise ValidationError("new_item_name is required when sku is not given.")
        category = get_category_by_code(conn, intake.new_item_category_code)
        if category is None:
            raise ValidationError(f"Unknown category_code: {intake.new_item_category_code!r}")

        consign_prefix = f"{CONSIGN_SKU_PREFIX_TAG}-{category.sku_prefix}"
        if intake.new_item_sku and intake.new_item_sku.strip():
            sku = intake.new_item_sku.strip().upper()
        else:
            base = f"{consign_prefix}-{slugify_name(name)}"
            existing = get_skus_for_prefix_slug(conn, base)
            sku = generate_sku(consign_prefix, name, existing)

        if sku_exists(conn, sku):
            raise DuplicateSkuError(sku)

        try:
            # Savepoint, not a plain execute — same reasoning as
            # inventory/items.py::create_item and
            # inventory/purchases.py::save_purchase()'s new-item insert:
            # under genuine concurrency the pre-check above can race, and
            # the real unique index is the actual backstop.
            with conn.begin_nested():
                item_id = conn.execute(
                    text(
                        "INSERT INTO items (sku, name, category_id, identity_mode, consignor_id) "
                        "VALUES (:sku, :name, :category_id, 'serialized', :consignor_id) RETURNING id"
                    ),
                    {
                        "sku": sku,
                        "name": name,
                        "category_id": category.id,
                        "consignor_id": intake.consignor_id,
                    },
                ).scalar_one()
        except IntegrityError as exc:
            raise DuplicateSkuError(sku) from exc
        item_name = name

    # --- Resolve serial IDs: given explicitly, or generated — same helper
    # inventory/purchases.py uses for an ordinary serialized purchase line. ---
    provided_serials = intake.serial_ids or []
    if provided_serials and len(provided_serials) != intake.quantity:
        raise ValidationError(
            f"{len(provided_serials)} serial id(s) provided but quantity is {intake.quantity}."
        )
    existing_count = count_serial_units_for_item(conn, item_id)
    if provided_serials:
        resolved_serials = [s.strip() for s in provided_serials]
    else:
        resolved_serials = generate_serials_for_line(sku, intake.quantity, existing_count)

    dupes = find_duplicate_keys(resolved_serials)
    if dupes:
        raise DuplicateSerialError(next(iter(dupes)))
    for serial in resolved_serials:
        if serial_exists(conn, serial):
            raise DuplicateSerialError(serial)

    photos = intake.photo_references or [None] * intake.quantity
    if len(photos) != intake.quantity:
        raise ValidationError(
            f"{len(photos)} photo reference(s) provided but quantity is {intake.quantity}."
        )

    for serial_id, photo_ref in zip(resolved_serials, photos):
        try:
            # Savepoint — same reasoning as inventory/purchases.py's own
            # serial-unit inserts: a genuine race is caught by the real
            # unique index without hard-aborting the caller's connection.
            with conn.begin_nested():
                conn.execute(
                    text(
                        """
                        INSERT INTO serial_units
                            (item_id, serial_id, acquired_cost, purchase_line_item_id, photo_reference)
                        VALUES (:item_id, :serial_id, 0, NULL, :photo_reference)
                        """
                    ),
                    {"item_id": item_id, "serial_id": serial_id, "photo_reference": photo_ref},
                )
        except IntegrityError as exc:
            raise DuplicateSerialError(serial_id) from exc

    return ConsignmentIntakeResult(
        item_id=item_id,
        sku=sku,
        item_name=item_name,
        consignor_id=intake.consignor_id,
        serial_ids=resolved_serials,
    )


# --------------------------------------------------------------------- #
# Selling a consigned unit — wraps the real deplete_serial_unit() and
# creates an unpaid reimbursement record in the same transaction.
# --------------------------------------------------------------------- #


@dataclass
class ConsignmentSaleResult:
    reimbursement_id: int
    serial_unit_id: int
    item_id: int
    sku: str
    serial_id: str
    consignor_id: int
    status: str
    depletion: SerialDepletionResult


def sell_consigned_unit(
    conn: Connection,
    serial_id: str,
    expected_sku: Optional[str] = None,
    sold_date: Optional[date_type] = None,
    reference: Optional[str] = None,
) -> ConsignmentSaleResult:
    """Marks one consigned serial/asset unit as sold AND records a new,
    unpaid consignor reimbursement for it, atomically (same transaction).

    Rejects (``ItemNotConsignedError``) if the targeted unit's item isn't
    tagged to any consignor at all — this function is specifically for
    consigned items; a plain owned-stock unit must go through
    ``inventory.depletions.deplete_serial_unit()`` directly instead (see
    webapp/api.py's single "Mark as Sold" entry point, which detects which
    one to call).

    Every other failure mode (unit not found, already sold, sku mismatch)
    is the SAME real error ``deplete_serial_unit()`` itself raises — this
    function never reimplements that logic, only wraps it.
    """
    normalized = (serial_id or "").strip()
    if not normalized:
        raise ValidationError("serial_id is required.")

    # Pre-check the item's consignor_id BEFORE touching inventory — a
    # consignor_id is write-once (see module docstring / migration 006's own
    # comment), so there's no race between this SELECT and the real atomic
    # depletion UPDATE below for this specific value. If no row matches this
    # serial_id + expected_sku combination at all, `row` stays None and the
    # real deplete_serial_unit() call below raises the SPECIFIC correct
    # error (not found / mismatch) via its own diagnostics — never
    # reimplemented here.
    row = conn.execute(
        text(
            """
            SELECT i.consignor_id
            FROM serial_units su
            JOIN items i ON i.id = su.item_id
            WHERE UPPER(su.serial_id) = UPPER(:serial_id)
              AND (:expected_sku IS NULL OR UPPER(i.sku) = UPPER(:expected_sku))
            """
        ),
        {"serial_id": normalized, "expected_sku": expected_sku},
    ).mappings().first()
    if row is not None and row["consignor_id"] is None:
        raise ItemNotConsignedError(expected_sku or normalized)
    consignor_id = row["consignor_id"] if row is not None else None

    depletion = deplete_serial_unit(
        conn, serial_id=normalized, expected_sku=expected_sku, sold_date=sold_date, reference=reference
    )
    # depletion succeeded, so the unit genuinely existed and matched the
    # given identity — meaning `row` above was necessarily found (same
    # identity match, consignor_id immutable), so consignor_id here is a
    # real, non-None value. Asserted rather than silently trusted, so a
    # future change to either query's matching logic can't silently drift.
    assert consignor_id is not None, "unreachable: depletion succeeded without a matching consignor pre-check row"

    try:
        with conn.begin_nested():
            reimbursement_id = conn.execute(
                text(
                    """
                    INSERT INTO consignor_reimbursements (serial_unit_id, consignor_id, reference)
                    VALUES (:serial_unit_id, :consignor_id, :reference)
                    RETURNING id
                    """
                ),
                {"serial_unit_id": depletion.id, "consignor_id": consignor_id, "reference": reference},
            ).scalar_one()
    except IntegrityError as exc:
        raise ReimbursementAlreadyExistsError(depletion.serial_id) from exc

    return ConsignmentSaleResult(
        reimbursement_id=reimbursement_id,
        serial_unit_id=depletion.id,
        item_id=depletion.item_id,
        sku=depletion.sku,
        serial_id=depletion.serial_id,
        consignor_id=consignor_id,
        status="unpaid",
        depletion=depletion,
    )


# --------------------------------------------------------------------- #
# Reimbursements — list/get, and the one genuinely new concurrency-
# sensitive invariant: the unpaid -> paid compare-and-swap.
# --------------------------------------------------------------------- #


@dataclass
class ReimbursementSummary:
    id: int
    serial_unit_id: int
    sku: str
    item_name: str
    serial_id: str
    consignor_id: int
    consignor_name: str
    status: str
    paid_date: Optional[date_type]
    reference: Optional[str]


_SELECT_REIMBURSEMENT = """
    SELECT cr.id, cr.serial_unit_id, cr.consignor_id, co.name AS consignor_name,
           cr.status, cr.paid_date, cr.reference,
           su.serial_id, i.sku, i.name AS item_name
    FROM consignor_reimbursements cr
    JOIN serial_units su ON su.id = cr.serial_unit_id
    JOIN items i ON i.id = su.item_id
    JOIN consignors co ON co.id = cr.consignor_id
"""


def _reimbursement_row_to_result(row) -> ReimbursementSummary:
    return ReimbursementSummary(
        id=row["id"],
        serial_unit_id=row["serial_unit_id"],
        sku=row["sku"],
        item_name=row["item_name"],
        serial_id=row["serial_id"],
        consignor_id=row["consignor_id"],
        consignor_name=row["consignor_name"],
        status=row["status"],
        paid_date=row["paid_date"],
        reference=row["reference"],
    )


def get_reimbursement(conn: Connection, reimbursement_id: int) -> ReimbursementSummary:
    row = conn.execute(
        text(_SELECT_REIMBURSEMENT + " WHERE cr.id = :id"), {"id": reimbursement_id}
    ).mappings().first()
    if row is None:
        raise ReimbursementNotFoundError(reimbursement_id)
    return _reimbursement_row_to_result(row)


def list_reimbursements(
    conn: Connection, consignor_id: Optional[int] = None, status: Optional[str] = None
) -> list[ReimbursementSummary]:
    rows = conn.execute(
        text(
            _SELECT_REIMBURSEMENT
            + " WHERE (:consignor_id IS NULL OR cr.consignor_id = :consignor_id) "
            + "AND (:status IS NULL OR cr.status = :status) "
            + "ORDER BY cr.id"
        ),
        {"consignor_id": consignor_id, "status": status},
    ).mappings().all()
    return [_reimbursement_row_to_result(r) for r in rows]


def mark_reimbursement_paid(
    conn: Connection,
    reimbursement_id: int,
    paid_date: Optional[date_type] = None,
    payment_reference: Optional[str] = None,
) -> ReimbursementSummary:
    """Marks a still-``unpaid`` reimbursement as ``paid`` via a plain atomic
    ``UPDATE ... WHERE status = 'unpaid'`` compare-and-swap — mirrors
    ``inventory/preorders.py::cancel_preorder_sale`` and Milestone 5's
    serialized-unit depletion path exactly, NEVER a trigger-based
    ``SELECT ... FOR UPDATE``. If the row is already ``paid`` (including the
    case where a concurrent mark-as-paid won the race a moment earlier),
    raises ``ReimbursementNotUnpaidError`` rather than silently no-op'ing.

    ``payment_reference``, if given, OVERWRITES the stored ``reference``
    field with a payment-specific note (e.g. a bank transfer reference) —
    if omitted, the reference set at sale time (``sell_consigned_unit``) is
    left untouched.
    """
    paid_date = paid_date or date_type.today()
    gated = conn.execute(
        text(
            """
            UPDATE consignor_reimbursements
            SET status = 'paid', paid_date = :paid_date, paid_at = now(),
                reference = COALESCE(:payment_reference, reference)
            WHERE id = :id AND status = 'unpaid'
            RETURNING id
            """
        ),
        {"id": reimbursement_id, "paid_date": paid_date, "payment_reference": payment_reference},
    ).first()

    if gated is None:
        # Best-effort, race-tolerant diagnostic lookup ONLY — same pattern
        # as inventory/preorders.py::cancel_preorder_sale. The real gate
        # already ran above; this just produces a specific, actionable
        # error message (not found vs. already paid).
        existing = conn.execute(
            text("SELECT status FROM consignor_reimbursements WHERE id = :id"), {"id": reimbursement_id}
        ).first()
        if existing is None:
            raise ReimbursementNotFoundError(reimbursement_id)
        raise ReimbursementNotUnpaidError(reimbursement_id, current_status=existing[0])

    return get_reimbursement(conn, reimbursement_id)
