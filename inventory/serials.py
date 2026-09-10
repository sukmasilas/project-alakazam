"""Serial/Asset ID generation — ports docs/design/mockup.html's
``generateSerialsForLine`` faithfully.

Convention (design doc Open Question 7, "not specified in the brief; a
reasonable placeholder"): ``{SKU}-{3-digit sequence}``, continuing from
however many units of that exact SKU already exist, so a later batch never
collides with an earlier one.

Pure (no DB access) — callers (inventory/purchases.py) supply
``existing_count`` (units of this SKU already posted, from the database)
and ``sibling_reserved`` (serial IDs already assigned to *other* lines of
the same SKU within the same in-flight purchase — mirrors the mockup's
"siblingMax" scan, which is what stops two lines of one SKU in one
purchase from both generating from the same stale starting count and
colliding with each other).
"""
from __future__ import annotations

import re
from typing import Iterable


def generate_serial_id(sku: str, sequence: int) -> str:
    return f"{sku}-{sequence:03d}"


def generate_serials_for_line(
    sku: str,
    quantity: int,
    existing_count: int,
    sibling_reserved: Iterable[str] = (),
) -> list[str]:
    """Returns ``quantity`` freshly generated serial IDs for ``sku``,
    starting after both ``existing_count`` (already-posted units) and the
    highest sequence number found among ``sibling_reserved`` (serials
    already assigned to sibling lines of the same SKU in this same
    unsaved purchase) — whichever is higher.
    """
    pattern = re.compile(rf"^{re.escape(sku)}-(\d{{3}})$", re.IGNORECASE)
    sibling_max = 0
    for serial in sibling_reserved:
        match = pattern.match(serial)
        if match:
            sibling_max = max(sibling_max, int(match.group(1)))

    start_at = max(existing_count, sibling_max)
    return [generate_serial_id(sku, start_at + i + 1) for i in range(quantity)]
