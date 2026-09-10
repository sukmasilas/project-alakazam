"""SKU generation — ports docs/design/mockup.html's ``generateSku`` /
``slugifyName`` faithfully.

Convention (design doc, "placeholder pending real user confirmation, not a
settled rule" — implemented exactly as documented, not silently changed):

    {CATEGORY_PREFIX}-{first 1-2 words of the item name, slugified}-{4-digit sequence}

e.g. "Charizard VMAX Box" in TCG -> ``TCG-CHARIZARD-VMAX-0001``.

The sequence is scoped to the prefix+slug combination, not the whole
category (design doc Open Question 6) — a second, unrelated TCG item
doesn't bump an existing name-slug family's counter, but a second
Charizard-VMAX item correctly becomes ``...-0002``.

This module is pure (no DB access) so it's cheap to unit test — callers
(inventory/purchases.py) are responsible for gathering the right
``existing_skus`` iterable: every item SKU already in the database sharing
this category's prefix, PLUS any SKU already assigned to another line in
the same in-flight purchase (mirrors the mockup's ``generateSku`` scanning
both ``itemsMaster`` and ``draftPurchase.lines``) — this is what stops two
new-item lines in one purchase from colliding with each other before either
has been saved.
"""
from __future__ import annotations

import re
from typing import Iterable

_NON_ALNUM_RUN = re.compile(r"[^A-Z0-9]+")


def slugify_name(name: str) -> str:
    """First 1-2 words of ``name``, upper-cased, non-alphanumeric runs
    collapsed to a single hyphen, leading/trailing hyphens stripped.
    Falls back to "ITEM" for an empty/whitespace-only name.
    """
    words = (name or "").strip().split()
    words = words[:2]
    joined = " ".join(words).upper()
    slug = _NON_ALNUM_RUN.sub("-", joined).strip("-")
    return slug or "ITEM"


def generate_sku(category_prefix: str, name: str, existing_skus: Iterable[str]) -> str:
    """Returns the next SKU for this category prefix + name-slug family,
    given every already-taken SKU that could plausibly collide (see module
    docstring for what ``existing_skus`` must include).
    """
    slug = slugify_name(name)
    base = f"{category_prefix}-{slug}"
    pattern = re.compile(rf"^{re.escape(base)}-(\d{{4}})$", re.IGNORECASE)

    max_seq = 0
    for sku in existing_skus:
        match = pattern.match(sku)
        if match:
            max_seq = max(max_seq, int(match.group(1)))

    return f"{base}-{max_seq + 1:04d}"
