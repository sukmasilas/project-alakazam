"""CSV row-shaping + text generation for the Noctrowl export.

See docs/design/milestone-4-export-design.md for the full column-by-column
rationale. Summary of the two rules that matter most for correctness:

1. This module never recomputes allocation math. ``allocated_item_cost``,
   ``shipping_share``, and ``line_total`` are always the exact, verbatim
   values already stored on ``purchase_line_items`` by
   ``inventory/purchases.py`` / ``inventory/allocation.py`` — read straight
   out of the database, never re-derived. For a serialized line with
   multiple units, these three columns repeat the same LINE-level values
   across every one of that line's unit-rows (the schema has no per-unit
   split of item-cost-vs-shipping to read instead — only the combined
   per-unit ``serial_units.acquired_cost``).
2. One row per costed unit (a serialized unit) or per fungible line. The
   ``unit_cost`` column is the one place a real per-row number is computed:
   for a fungible row it's ``line_total / quantity`` (unrounded Decimal
   division, same convention already used by
   ``inventory/queries.py::get_fungible_purchase_rows``); for a serialized
   row it's simply that unit's own stored ``acquired_cost`` — not derived,
   just read.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Column order is part of the export contract with Noctrowl — see the
# design doc. Keep this list and ExportRow's field order in sync.
EXPORT_COLUMNS = [
    "purchase_id",
    "purchase_line_item_id",
    "serial_unit_id",
    "purchase_ref",
    "purchase_date",
    "vendor_description",
    "item_id",
    "sku",
    "item_name",
    "category",
    "identity_mode",
    "serial_identifier",
    "quantity",
    "pricing_mode",
    "shipping_costing_method",
    "allocated_item_cost",
    "shipping_share",
    "line_total",
    "unit_cost",
    "currency",
    "fx_rate_to_idr",
]


@dataclass
class ExportRow:
    purchase_id: int
    purchase_line_item_id: int
    serial_unit_id: Optional[int]
    purchase_ref: str
    purchase_date: date_type
    vendor_description: str
    item_id: int
    sku: str
    item_name: str
    category: str
    identity_mode: str
    serial_identifier: Optional[str]
    quantity: int
    pricing_mode: str
    shipping_costing_method: str
    allocated_item_cost: Decimal
    shipping_share: Decimal
    line_total: Decimal
    unit_cost: Decimal
    currency: str
    fx_rate_to_idr: Optional[Decimal]

    def to_csv_dict(self) -> dict:
        """String-ified representation for csv.DictWriter — money as plain
        decimal strings (never float), nulls as empty string (CSV has no
        real null), dates as ISO-8601.
        """
        return {
            "purchase_id": str(self.purchase_id),
            "purchase_line_item_id": str(self.purchase_line_item_id),
            "serial_unit_id": "" if self.serial_unit_id is None else str(self.serial_unit_id),
            "purchase_ref": self.purchase_ref,
            "purchase_date": self.purchase_date.isoformat(),
            "vendor_description": self.vendor_description,
            "item_id": str(self.item_id),
            "sku": self.sku,
            "item_name": self.item_name,
            "category": self.category,
            "identity_mode": self.identity_mode,
            "serial_identifier": self.serial_identifier or "",
            "quantity": str(self.quantity),
            "pricing_mode": self.pricing_mode,
            "shipping_costing_method": self.shipping_costing_method,
            "allocated_item_cost": str(self.allocated_item_cost),
            "shipping_share": str(self.shipping_share),
            "line_total": str(self.line_total),
            "unit_cost": str(self.unit_cost),
            "currency": self.currency,
            "fx_rate_to_idr": "" if self.fx_rate_to_idr is None else str(self.fx_rate_to_idr),
        }


def _shipping_costing_method(purchase_shipping_mode: str, ships_separately: bool) -> str:
    """The EFFECTIVE per-line shipping costing method, not just a blind
    copy of the purchase header's shipping_mode — a line inside a 'pooled'
    purchase can still opt out via ships_separately and get its own manual
    amount instead (inventory/allocation.py's compute_purchase_allocation).
    Reading this correctly per-line matters for an honest audit trail: a
    reader scanning this column should see how THIS line's shipping was
    actually costed, not the purchase-wide default.
    """
    if purchase_shipping_mode == "pooled" and ships_separately:
        return "manual"
    return purchase_shipping_mode


_FUNGIBLE_QUERY = text(
    """
    SELECT
        p.id AS purchase_id, p.purchase_ref, p.purchase_date, p.vendor_description,
        p.currency, p.fx_rate_to_idr, p.shipping_mode AS purchase_shipping_mode,
        i.id AS item_id, i.sku, i.name AS item_name, c.name AS category_name,
        i.identity_mode,
        pli.id AS purchase_line_item_id, pli.quantity, pli.pricing_mode,
        pli.ships_separately, pli.allocated_item_cost, pli.shipping_share, pli.line_total
    FROM purchase_line_items pli
    JOIN purchases p ON p.id = pli.purchase_id
    JOIN items i ON i.id = pli.item_id
    JOIN categories c ON c.id = i.category_id
    WHERE i.identity_mode = 'fungible'
      AND (:purchase_ids IS NULL OR p.id = ANY(:purchase_ids))
    ORDER BY p.id, pli.id
    """
)

_SERIALIZED_QUERY = text(
    """
    SELECT
        p.id AS purchase_id, p.purchase_ref, p.purchase_date, p.vendor_description,
        p.currency, p.fx_rate_to_idr, p.shipping_mode AS purchase_shipping_mode,
        i.id AS item_id, i.sku, i.name AS item_name, c.name AS category_name,
        i.identity_mode,
        pli.id AS purchase_line_item_id, pli.quantity, pli.pricing_mode,
        pli.ships_separately, pli.allocated_item_cost, pli.shipping_share, pli.line_total,
        su.id AS serial_unit_id, su.serial_id, su.acquired_cost
    FROM serial_units su
    JOIN purchase_line_items pli ON pli.id = su.purchase_line_item_id
    JOIN purchases p ON p.id = pli.purchase_id
    JOIN items i ON i.id = pli.item_id
    JOIN categories c ON c.id = i.category_id
    WHERE i.identity_mode = 'serialized'
      AND (:purchase_ids IS NULL OR p.id = ANY(:purchase_ids))
    ORDER BY p.id, pli.id, su.id
    """
)


def build_export_rows(conn: Connection, purchase_ids: Optional[list[int]] = None) -> list[ExportRow]:
    """Every export row for the given purchase ids (or every purchase in
    the database, if ``purchase_ids`` is None), fungible and serialized
    lines merged and ordered by (purchase_id, purchase_line_item_id,
    serial_unit_id) so a purchase's rows always appear together and in a
    stable, deterministic order.
    """
    rows: list[ExportRow] = []

    fungible_rows = conn.execute(_FUNGIBLE_QUERY, {"purchase_ids": purchase_ids}).mappings().all()
    for r in fungible_rows:
        line_total = Decimal(r["line_total"])
        quantity = int(r["quantity"])
        unit_cost = (line_total / quantity) if quantity else Decimal(0)
        rows.append(
            ExportRow(
                purchase_id=r["purchase_id"],
                purchase_line_item_id=r["purchase_line_item_id"],
                serial_unit_id=None,
                purchase_ref=r["purchase_ref"],
                purchase_date=r["purchase_date"],
                vendor_description=r["vendor_description"],
                item_id=r["item_id"],
                sku=r["sku"],
                item_name=r["item_name"],
                category=r["category_name"],
                identity_mode=r["identity_mode"],
                serial_identifier=None,
                quantity=quantity,
                pricing_mode=r["pricing_mode"],
                shipping_costing_method=_shipping_costing_method(
                    r["purchase_shipping_mode"], r["ships_separately"]
                ),
                allocated_item_cost=Decimal(r["allocated_item_cost"]),
                shipping_share=Decimal(r["shipping_share"]),
                line_total=line_total,
                unit_cost=unit_cost,
                currency=r["currency"],
                fx_rate_to_idr=(
                    None if r["fx_rate_to_idr"] is None else Decimal(r["fx_rate_to_idr"])
                ),
            )
        )

    serialized_rows = conn.execute(_SERIALIZED_QUERY, {"purchase_ids": purchase_ids}).mappings().all()
    for r in serialized_rows:
        rows.append(
            ExportRow(
                purchase_id=r["purchase_id"],
                purchase_line_item_id=r["purchase_line_item_id"],
                serial_unit_id=r["serial_unit_id"],
                purchase_ref=r["purchase_ref"],
                purchase_date=r["purchase_date"],
                vendor_description=r["vendor_description"],
                item_id=r["item_id"],
                sku=r["sku"],
                item_name=r["item_name"],
                category=r["category_name"],
                identity_mode=r["identity_mode"],
                serial_identifier=r["serial_id"],
                quantity=1,
                pricing_mode=r["pricing_mode"],
                shipping_costing_method=_shipping_costing_method(
                    r["purchase_shipping_mode"], r["ships_separately"]
                ),
                allocated_item_cost=Decimal(r["allocated_item_cost"]),
                shipping_share=Decimal(r["shipping_share"]),
                line_total=Decimal(r["line_total"]),
                unit_cost=Decimal(r["acquired_cost"]),
                currency=r["currency"],
                fx_rate_to_idr=(
                    None if r["fx_rate_to_idr"] is None else Decimal(r["fx_rate_to_idr"])
                ),
            )
        )

    rows.sort(key=lambda row: (row.purchase_id, row.purchase_line_item_id, row.serial_unit_id or 0))
    return rows


def rows_to_csv_text(rows: list[ExportRow]) -> str:
    """Renders rows to CSV text (header + data), CRLF line endings per RFC
    4180 (csv module's default), UTF-8-safe (no encoding done here — the
    caller encodes to bytes for upload, see export/runner.py).
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row.to_csv_dict())
    return buffer.getvalue()


def build_export_csv(conn: Connection, purchase_ids: Optional[list[int]] = None) -> tuple[str, list[ExportRow]]:
    """Convenience wrapper: builds rows, renders CSV text, and returns both
    (the runner needs the rows too, to know which purchase ids to mark
    exported — deriving that from the CSV text back would be silly).
    """
    rows = build_export_rows(conn, purchase_ids=purchase_ids)
    return rows_to_csv_text(rows), rows
