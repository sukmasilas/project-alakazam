"""Orchestration layer for eBay sales CSV import + the review-queue
confirm/skip/process flow — Milestone 6.

Every function here calls straight into ``inventory.depletions`` for the
real inventory movement — no depletion math or invariant is reimplemented
at this layer (same discipline ``webapp/api.py`` already follows for
purchases/depletions).

State machine for ``ebay_sales_rows.review_status`` (Order rows only —
Refund rows stay ``pending`` forever and are never mutated by any function
here, see ``mark_row_matched``/``mark_row_skipped``'s own guards):

    pending --(mark_row_matched)--> matched --(process_confirmed_rows)--> posted
    pending --(mark_row_skipped)--> skipped
    matched --(mark_row_skipped)--> skipped
    matched --(mark_row_matched, re-pick)--> matched

``posted`` is terminal — no function here ever moves a row out of
``posted`` (see CLAUDE.md: corrections to an already-posted row are out of
scope for this prototype, same policy as the sibling Noctrowl project).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as date_type
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ingestion.ebay_csv import parse_ebay_transaction_report
from ingestion.fuzzy_match import suggest_item_matches
from inventory.depletions import deplete_fungible, deplete_serial_unit
from inventory.exceptions import AlakazamError
from inventory.queries import get_item_by_sku, get_serialized_unit_rows

_KNOWN_STORED_TYPES = {"Order", "Refund"}


class RowNotFoundError(Exception):
    def __init__(self, row_id: int):
        self.row_id = row_id
        super().__init__(f"No eBay sales row with id {row_id}.")


class InvalidRowActionError(Exception):
    """Raised for a request that's structurally invalid for the target
    row's current state (e.g. matching a Refund row, re-matching a row
    that's already posted, a serial-unit pick count that doesn't equal the
    row's quantity). Distinct from an ``AlakazamError`` — these never reach
    the depletion engine at all.
    """


class RowAlreadyProcessedError(Exception):
    """Raised internally by ``process_confirmed_rows`` when a row's status
    is no longer ``matched`` by the moment its own turn comes to post
    (e.g. a concurrent process-call already claimed it a moment earlier).
    Caught at the same per-row boundary as an ``AlakazamError`` — never
    propagates out of ``process_confirmed_rows`` itself, never aborts the
    rest of the batch.
    """

    def __init__(self, row_id: int):
        self.row_id = row_id
        super().__init__(
            f"eBay sales row {row_id} is no longer in 'matched' status — "
            "it may have already been processed or skipped concurrently."
        )


# --------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------- #


@dataclass
class ImportSummary:
    batch_id: int
    source_filename: str
    seller: Optional[str]
    total_rows_seen: int
    order_rows_stored: int
    order_rows_duplicate: int
    order_rows_missing_transaction_id: int
    order_summary_rows_skipped: int
    refund_rows_stored: int
    non_actionable_rows_skipped: int
    type_counts: dict = field(default_factory=dict)


def import_csv(conn: Connection, filename: str, csv_text: str) -> ImportSummary:
    """Parses and stores one uploaded eBay Transaction report. Idempotent
    against re-upload / overlapping months: a row whose ``Transaction ID``
    already exists for another ``Order`` row (any batch, not just this
    file) is silently NOT re-inserted — counted as a duplicate, never
    turned into a second reviewable row (see the real DB-level unique
    index in migrations/004_add_ebay_sales_import.sql, which is the actual
    backstop this relies on; the try/except below is just how that
    backstop's IntegrityError gets turned into a clean per-row skip rather
    than aborting the whole import).
    """
    parsed = parse_ebay_transaction_report(csv_text)

    batch_id = conn.execute(
        text(
            "INSERT INTO ebay_import_batches (source_filename, seller) "
            "VALUES (:filename, :seller) RETURNING id"
        ),
        {"filename": filename, "seller": parsed.seller},
    ).scalar_one()

    order_stored = 0
    order_dupe = 0
    order_missing_txn = 0
    refund_stored = 0
    non_actionable = sum(
        count for row_type, count in parsed.type_counts.items() if row_type not in _KNOWN_STORED_TYPES
    )

    for row in parsed.rows:
        if row.row_type == "Order" and row.ebay_transaction_id is None:
            # A real Order line-item row with no Transaction ID would be
            # un-idempotent to store (see migration 004's partial unique
            # index — it can't protect a NULL value). Confirmed 0/389 real
            # sample rows hit this case, but the parser can't guarantee a
            # future export never will — refuse to store rather than
            # silently accept a row this milestone can't promise won't
            # double-post on a later re-upload.
            order_missing_txn += 1
            continue

        try:
            with conn.begin_nested():
                conn.execute(
                    text(
                        """
                        INSERT INTO ebay_sales_rows
                            (batch_id, row_type, transaction_date, transaction_date_raw,
                             order_number, item_id, ebay_transaction_id, item_title,
                             custom_label, quantity)
                        VALUES
                            (:batch_id, :row_type, :transaction_date, :transaction_date_raw,
                             :order_number, :item_id, :ebay_transaction_id, :item_title,
                             :custom_label, :quantity)
                        """
                    ),
                    {
                        "batch_id": batch_id,
                        "row_type": row.row_type,
                        "transaction_date": row.transaction_date,
                        "transaction_date_raw": row.transaction_date_raw,
                        "order_number": row.order_number,
                        "item_id": row.item_id,
                        "ebay_transaction_id": row.ebay_transaction_id,
                        "item_title": row.item_title,
                        "custom_label": row.custom_label,
                        "quantity": row.quantity,
                    },
                )
        except IntegrityError as exc:
            if _is_order_txn_id_unique_violation(exc):
                order_dupe += 1
                continue
            raise  # pragma: no cover — any other IntegrityError is a real, unexpected failure

        if row.row_type == "Order":
            order_stored += 1
        else:
            refund_stored += 1

    conn.execute(
        text(
            """
            UPDATE ebay_import_batches SET
                total_rows_seen = :total_rows_seen,
                order_rows_stored = :order_rows_stored,
                order_rows_duplicate = :order_rows_duplicate,
                order_summary_rows_skipped = :order_summary_rows_skipped,
                refund_rows_stored = :refund_rows_stored,
                non_actionable_rows_skipped = :non_actionable_rows_skipped
            WHERE id = :batch_id
            """
        ),
        {
            "batch_id": batch_id,
            "total_rows_seen": sum(parsed.type_counts.values()),
            "order_rows_stored": order_stored,
            "order_rows_duplicate": order_dupe,
            "order_summary_rows_skipped": parsed.order_summary_rows_skipped,
            "refund_rows_stored": refund_stored,
            "non_actionable_rows_skipped": non_actionable,
        },
    )

    return ImportSummary(
        batch_id=batch_id,
        source_filename=filename,
        seller=parsed.seller,
        total_rows_seen=sum(parsed.type_counts.values()),
        order_rows_stored=order_stored,
        order_rows_duplicate=order_dupe,
        order_rows_missing_transaction_id=order_missing_txn,
        order_summary_rows_skipped=parsed.order_summary_rows_skipped,
        refund_rows_stored=refund_stored,
        non_actionable_rows_skipped=non_actionable,
        type_counts=parsed.type_counts,
    )


def _is_order_txn_id_unique_violation(exc: IntegrityError) -> bool:
    """True only for the specific
    ``ux_ebay_sales_rows_order_txn_id`` unique-index violation — never a
    blanket "any IntegrityError means duplicate" (that would silently mask
    a real, different data-integrity bug as a harmless duplicate).
    """
    orig = getattr(exc, "orig", None)
    pgcode = getattr(orig, "pgcode", None)
    if pgcode != "23505":
        return False
    diag = getattr(orig, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    return constraint_name == "ux_ebay_sales_rows_order_txn_id"


# --------------------------------------------------------------------- #
# Review queue reads
# --------------------------------------------------------------------- #


@dataclass
class ReviewRow:
    id: int
    batch_id: int
    row_type: str
    transaction_date: Optional[date_type]
    order_number: Optional[str]
    item_id: Optional[str]
    ebay_transaction_id: Optional[str]
    item_title: str
    custom_label: Optional[str]
    quantity: Optional[int]
    review_status: str
    matched_item_sku: Optional[str]
    matched_item_name: Optional[str]
    matched_identity_mode: Optional[str]
    matched_serial_ids: Optional[list]
    last_process_error: Optional[str]
    suggestions: list = field(default_factory=list)


_REVIEW_ROW_SELECT = """
    SELECT r.id, r.batch_id, r.row_type, r.transaction_date, r.order_number, r.item_id,
           r.ebay_transaction_id, r.item_title, r.custom_label, r.quantity,
           r.review_status, r.matched_serial_ids, r.last_process_error,
           i.sku AS matched_item_sku, i.name AS matched_item_name, i.identity_mode AS matched_identity_mode
    FROM ebay_sales_rows r
    LEFT JOIN items i ON i.id = r.matched_item_id
"""


def _row_from_mapping(mapping, suggestions: list) -> ReviewRow:
    return ReviewRow(
        id=mapping["id"],
        batch_id=mapping["batch_id"],
        row_type=mapping["row_type"],
        transaction_date=mapping["transaction_date"],
        order_number=mapping["order_number"],
        item_id=mapping["item_id"],
        ebay_transaction_id=mapping["ebay_transaction_id"],
        item_title=mapping["item_title"],
        custom_label=mapping["custom_label"],
        quantity=mapping["quantity"],
        review_status=mapping["review_status"],
        matched_item_sku=mapping["matched_item_sku"],
        matched_item_name=mapping["matched_item_name"],
        matched_identity_mode=mapping["matched_identity_mode"],
        matched_serial_ids=mapping["matched_serial_ids"],
        last_process_error=mapping["last_process_error"],
        suggestions=suggestions,
    )


def list_review_rows(
    conn: Connection, batch_id: Optional[int] = None, include_suggestions: bool = True
) -> list[ReviewRow]:
    """Every stored row (Order + Refund), newest-imported-last (stable id
    order) — Order rows still ``pending`` get a live fuzzy-match suggestion
    list attached (advisory only, see ingestion/fuzzy_match.py).
    """
    rows = conn.execute(
        text(_REVIEW_ROW_SELECT + " WHERE (:batch_id IS NULL OR r.batch_id = :batch_id) ORDER BY r.id"),
        {"batch_id": batch_id},
    ).mappings().all()

    results = []
    for row in rows:
        suggestions = []
        if include_suggestions and row["row_type"] == "Order" and row["review_status"] == "pending":
            candidates = suggest_item_matches(conn, row["item_title"])
            suggestions = [
                {
                    "sku": c.sku,
                    "name": c.name,
                    "category_code": c.category_code,
                    "identity_mode": c.identity_mode,
                    "score": c.score,
                }
                for c in candidates
            ]
        results.append(_row_from_mapping(row, suggestions))
    return results


def get_review_row(conn: Connection, row_id: int) -> ReviewRow:
    row = conn.execute(text(_REVIEW_ROW_SELECT + " WHERE r.id = :id"), {"id": row_id}).mappings().first()
    if row is None:
        raise RowNotFoundError(row_id)
    return _row_from_mapping(row, [])


def list_batches(conn: Connection) -> list[dict]:
    rows = conn.execute(
        text(
            """
            SELECT id, source_filename, seller, uploaded_at, total_rows_seen,
                   order_rows_stored, order_rows_duplicate, order_summary_rows_skipped,
                   refund_rows_stored, non_actionable_rows_skipped
            FROM ebay_import_batches ORDER BY id DESC
            """
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def get_on_hand_serials_for_sku(conn: Connection, sku: str) -> list[dict]:
    """The picker source for a serialized match — only currently on_hand
    units, since the CSV never tells you which unit sold (see CLAUDE.md).
    """
    item = get_item_by_sku(conn, sku)
    if item is None or item.identity_mode != "serialized":
        return []
    units = get_serialized_unit_rows(conn, item.id)
    return [{"serial_id": u.serial_id, "cost": str(u.cost)} for u in units if u.status == "on_hand"]


# --------------------------------------------------------------------- #
# Review queue writes: match / skip
# --------------------------------------------------------------------- #


def _load_row_for_mutation(conn: Connection, row_id: int) -> dict:
    row = conn.execute(
        text("SELECT id, row_type, quantity, review_status FROM ebay_sales_rows WHERE id = :id"),
        {"id": row_id},
    ).mappings().first()
    if row is None:
        raise RowNotFoundError(row_id)
    return dict(row)


def mark_row_matched(
    conn: Connection, row_id: int, sku: str, serial_ids: Optional[list[str]] = None
) -> ReviewRow:
    """Stages a human-confirmed match — does NOT post anything to
    inventory yet (that only happens via ``process_confirmed_rows``, a
    separate, explicit action). For a serialized item, ``serial_ids`` must
    contain exactly ``quantity`` distinct, non-empty on-hand unit picks —
    checked here for a fast, clear error, but the real on-hand gate is
    still ``deplete_serial_unit``'s own atomic UPDATE at process time (this
    check can go stale between matching and processing, same as every
    other pre-check in this codebase).
    """
    row = _load_row_for_mutation(conn, row_id)
    if row["row_type"] != "Order":
        raise InvalidRowActionError("Only Order rows can be matched to an item — Refund rows are visibility-only.")
    if row["review_status"] == "posted":
        raise InvalidRowActionError(
            "This row has already been posted to inventory — corrections to a posted row "
            "are out of scope for this prototype."
        )

    item = get_item_by_sku(conn, sku)
    if item is None:
        raise InvalidRowActionError(f"No item with SKU {sku!r}.")

    quantity = row["quantity"] or 1
    normalized_serials: Optional[list[str]] = None
    if item.identity_mode == "serialized":
        candidates = [s.strip() for s in (serial_ids or []) if s and s.strip()]
        if len(candidates) != quantity:
            raise InvalidRowActionError(
                f"Item {sku!r} is serialized — this row needs exactly {quantity} on-hand unit "
                f"selection(s), got {len(candidates)}."
            )
        if len(set(s.upper() for s in candidates)) != len(candidates):
            raise InvalidRowActionError("The same Serial/Asset unit was selected more than once for this row.")
        normalized_serials = candidates

    conn.execute(
        text(
            """
            UPDATE ebay_sales_rows
            SET review_status = 'matched', matched_item_id = :item_id,
                matched_serial_ids = CAST(:serial_ids AS JSONB), last_process_error = NULL
            WHERE id = :id
            """
        ),
        {
            "item_id": item.id,
            "serial_ids": json.dumps(normalized_serials) if normalized_serials is not None else None,
            "id": row_id,
        },
    )
    return get_review_row(conn, row_id)


def mark_row_skipped(conn: Connection, row_id: int) -> ReviewRow:
    row = _load_row_for_mutation(conn, row_id)
    if row["row_type"] != "Order":
        raise InvalidRowActionError("Only Order rows can be skipped — Refund rows never require an action.")
    if row["review_status"] == "posted":
        raise InvalidRowActionError("This row has already been posted to inventory and can no longer be skipped.")

    conn.execute(
        text(
            """
            UPDATE ebay_sales_rows
            SET review_status = 'skipped', matched_item_id = NULL,
                matched_serial_ids = NULL, last_process_error = NULL
            WHERE id = :id
            """
        ),
        {"id": row_id},
    )
    return get_review_row(conn, row_id)


# --------------------------------------------------------------------- #
# Process confirmed ("matched") rows — the only place a real depletion
# ever gets posted from this milestone.
# --------------------------------------------------------------------- #


@dataclass
class ProcessResult:
    row_id: int
    ebay_transaction_id: Optional[str]
    item_title: str
    success: bool
    error_type: Optional[str] = None
    error: Optional[str] = None
    fungible_depletion_id: Optional[int] = None
    serial_ids_depleted: list = field(default_factory=list)


def process_confirmed_rows(conn: Connection, batch_id: Optional[int] = None) -> list[ProcessResult]:
    """Calls the real Milestone 5 engine for every row currently
    ``matched`` (optionally narrowed to one batch), one at a time, each
    wrapped in its own savepoint — a failure on one row (e.g. the matched
    item went out of stock since it was matched) never rolls back another
    row's already-successful posting within the same call, and never
    aborts the rest of the batch. Reports one ``ProcessResult`` per row.

    Each row's savepoint does the real work in this order: (1) an atomic
    ``review_status = 'matched' -> 'posted'`` UPDATE, gated on the row
    STILL being 'matched' (the real protection against double-processing
    the same row, e.g. two concurrent "Process confirmed rows" clicks —
    see CLAUDE.md's idempotency requirement); (2) the real depletion
    call(s). If either step raises, the WHOLE savepoint (including the
    'posted' flip from step 1) rolls back automatically, so a failed row
    reverts cleanly to 'matched' for retry — no separate "undo" code path
    needed.
    """
    matched_rows = conn.execute(
        text(
            """
            SELECT r.id, r.ebay_transaction_id, r.item_title, r.order_number, r.quantity,
                   r.matched_serial_ids, i.sku AS matched_sku, i.identity_mode
            FROM ebay_sales_rows r
            JOIN items i ON i.id = r.matched_item_id
            WHERE r.review_status = 'matched' AND (:batch_id IS NULL OR r.batch_id = :batch_id)
            ORDER BY r.id
            """
        ),
        {"batch_id": batch_id},
    ).mappings().all()

    results: list[ProcessResult] = []
    for row in matched_rows:
        reference = f"eBay order {row['order_number']} / txn {row['ebay_transaction_id']}"
        fungible_depletion_id: Optional[int] = None
        serial_ids_depleted: list[str] = []
        try:
            with conn.begin_nested():
                gated = conn.execute(
                    text(
                        "UPDATE ebay_sales_rows SET review_status = 'posted' "
                        "WHERE id = :id AND review_status = 'matched' RETURNING id"
                    ),
                    {"id": row["id"]},
                ).first()
                if gated is None:
                    raise RowAlreadyProcessedError(row["id"])

                if row["identity_mode"] == "fungible":
                    depletion = deplete_fungible(
                        conn,
                        sku=row["matched_sku"],
                        quantity=row["quantity"],
                        reference=reference,
                        ebay_transaction_id=row["ebay_transaction_id"],
                    )
                    conn.execute(
                        text(
                            "INSERT INTO ebay_sales_row_depletions (ebay_sales_row_id, fungible_depletion_id) "
                            "VALUES (:row_id, :dep_id)"
                        ),
                        {"row_id": row["id"], "dep_id": depletion.id},
                    )
                    fungible_depletion_id = depletion.id
                else:
                    serial_ids = row["matched_serial_ids"] or []
                    for i, serial_id in enumerate(serial_ids):
                        suffix = (
                            row["ebay_transaction_id"]
                            if len(serial_ids) == 1
                            else f"{row['ebay_transaction_id']}-unit{i + 1}"
                        )
                        unit = deplete_serial_unit(
                            conn,
                            serial_id=serial_id,
                            expected_sku=row["matched_sku"],
                            reference=reference,
                            ebay_transaction_id=suffix,
                        )
                        conn.execute(
                            text(
                                "INSERT INTO ebay_sales_row_depletions (ebay_sales_row_id, serial_unit_id) "
                                "VALUES (:row_id, :unit_id)"
                            ),
                            {"row_id": row["id"], "unit_id": unit.id},
                        )
                        serial_ids_depleted.append(serial_id)
        except (AlakazamError, RowAlreadyProcessedError) as exc:
            conn.execute(
                text("UPDATE ebay_sales_rows SET last_process_error = :err WHERE id = :id"),
                {"err": str(exc), "id": row["id"]},
            )
            results.append(
                ProcessResult(
                    row_id=row["id"],
                    ebay_transaction_id=row["ebay_transaction_id"],
                    item_title=row["item_title"],
                    success=False,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            )
        else:
            results.append(
                ProcessResult(
                    row_id=row["id"],
                    ebay_transaction_id=row["ebay_transaction_id"],
                    item_title=row["item_title"],
                    success=True,
                    fungible_depletion_id=fungible_depletion_id,
                    serial_ids_depleted=serial_ids_depleted,
                )
            )
    return results
