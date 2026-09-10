"""Parses a real eBay Seller Hub "Transaction report" CSV export.

Format confirmed against real sample data (see CLAUDE.md's Milestone 6
decision and ``tests/ingestion/test_ebay_csv.py``, which parses the actual
sample files, not synthetic fixtures alone):

- A multi-line notes preamble BEFORE the real header row — the header row
  is found programmatically (the first line starting with
  ``"Transaction creation date,"``), never assumed to be row 1.
- Row ``Type`` values seen in real data: ``Order``, ``Refund``, ``Hold``,
  ``Other fee``, ``Payout``. Only ``Order`` and ``Refund`` are ever stored
  here (see ``parse_ebay_transaction_report``'s docstring for why) —
  everything else is still counted (``type_counts``) so nothing is
  silently discarded from view, just not modeled as its own row (none of
  it represents physical stock movement).
- A multi-item order produces ONE "Order" row with the net amount but no
  Item ID / Transaction ID / Item title (a rollup row, confirmed against
  real data: order 17-15092-65817 in the Aug 2026 sample), PLUS one
  "Order" row per actual line item (each with its own Transaction ID).
  Only the per-line-item rows carry matchable data — the rollup row is
  skipped (counted separately, ``order_summary_rows_skipped``).
- ``Transaction ID`` is confirmed unique among real per-line-item "Order"
  rows (389/389 unique across all 5 real sample files checked — 4 months
  of eBay account 1 plus 1 month of eBay account 2), but is NOT globally
  unique across every row type: a real cross-month example exists where a
  "Hold" row (Jul 2026) legitimately reuses the exact same Transaction ID
  as an unrelated, already-counted "Order" row from an earlier month (Jun
  2026, order 01-14825-30000) — a Hold-placed/Hold-released pair against
  an order under review. This is exactly why this milestone's real
  uniqueness invariant (see migrations/004_add_ebay_sales_import.sql) is
  scoped to ``row_type = 'Order'`` rows only, not a bare global constraint.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date as date_type, datetime
from typing import Optional

_HEADER_PREFIX = "Transaction creation date,"


@dataclass
class ParsedEbaySalesRow:
    row_type: str  # 'Order' | 'Refund'
    transaction_date: Optional[date_type]
    transaction_date_raw: str
    order_number: Optional[str]
    item_id: Optional[str]
    ebay_transaction_id: Optional[str]
    item_title: str
    custom_label: Optional[str]
    quantity: Optional[int]


@dataclass
class ParsedCsvResult:
    seller: Optional[str]
    rows: list[ParsedEbaySalesRow]
    # Every Type value seen in the raw file, including ones never stored as
    # their own ebay_sales_rows row (Hold / Other fee / Payout / etc.) —
    # see module docstring. Keys are the raw Type string as it appears in
    # the CSV.
    type_counts: dict = field(default_factory=dict)
    order_summary_rows_skipped: int = 0


class EbayCsvFormatError(ValueError):
    """Raised when the file doesn't look like a real eBay Transaction
    report at all (no header row found) — never silently parsed as
    zero rows, which would look identical to "an empty but valid month".
    """


def _find_header_index(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        if line.startswith(_HEADER_PREFIX):
            return i
    raise EbayCsvFormatError(
        "Could not find the 'Transaction creation date' header row in this file — "
        "it doesn't look like a real eBay Seller Hub Transaction report export."
    )


def _extract_seller(preamble_lines: list[str]) -> Optional[str]:
    for line in preamble_lines:
        if line.startswith("Seller,"):
            _, _, rest = line.partition(",")
            rest = rest.strip().strip('"')
            return rest or None
    return None


def _clean(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip()
    return None if v in ("", "--") else v


def _parse_date(raw: Optional[str]) -> Optional[date_type]:
    v = _clean(raw)
    if v is None:
        return None
    try:
        return datetime.strptime(v, "%b %d, %Y").date()
    except ValueError:
        return None


def _parse_int(raw: Optional[str]) -> Optional[int]:
    v = _clean(raw)
    if v is None:
        return None
    v = v.replace(",", "")
    try:
        return int(float(v))
    except ValueError:
        return None


def parse_ebay_transaction_report(csv_text: str) -> ParsedCsvResult:
    """Parses raw CSV text (already decoded — callers handle the file's own
    encoding, typically utf-8-sig for a real eBay export's BOM) into
    actionable rows.

    Only two ``Type`` values ever become a stored row:
      - ``Order`` rows WITH a real ``Item title`` (the per-line-item detail
        rows) — everything Milestone 6's review queue and matching/
        depletion flow operates on.
      - ``Refund`` rows — kept for visibility only (CLAUDE.md: "surfaced
        but never auto-processed"), never matched or depleted.
    Every other row (Hold / Other fee / Payout / an "Order" rollup row with
    no item detail) is counted in the returned summary but not modeled as
    its own ``ParsedEbaySalesRow`` — none of it represents physical stock
    movement, so there's nothing for this project's inventory ledger to do
    with it (see CLAUDE.md's Milestone 6 decision).
    """
    lines = csv_text.splitlines()
    header_idx = _find_header_index(lines)
    seller = _extract_seller(lines[:header_idx])

    reader = csv.DictReader(lines[header_idx:])

    type_counts: dict[str, int] = {}
    order_summary_rows_skipped = 0
    rows: list[ParsedEbaySalesRow] = []

    for raw_row in reader:
        row_type = _clean(raw_row.get("Type"))
        if row_type is None:
            continue  # a stray blank/malformed trailing line

        type_counts[row_type] = type_counts.get(row_type, 0) + 1

        item_title = _clean(raw_row.get("Item title"))
        if row_type == "Order":
            if item_title is None:
                order_summary_rows_skipped += 1
                continue
        elif row_type != "Refund":
            continue

        raw_date = raw_row.get("Transaction creation date", "")
        rows.append(
            ParsedEbaySalesRow(
                row_type=row_type,
                transaction_date=_parse_date(raw_date),
                transaction_date_raw=(raw_date or "").strip(),
                order_number=_clean(raw_row.get("Order number")),
                item_id=_clean(raw_row.get("Item ID")),
                ebay_transaction_id=_clean(raw_row.get("Transaction ID")),
                item_title=item_title or "(no item title on this row)",
                custom_label=_clean(raw_row.get("Custom label")),
                quantity=_parse_int(raw_row.get("Quantity")),
            )
        )

    return ParsedCsvResult(
        seller=seller,
        rows=rows,
        type_counts=type_counts,
        order_summary_rows_skipped=order_summary_rows_skipped,
    )
