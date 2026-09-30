"""SKU generation — ports docs/design/mockup.html's ``generateSku`` /
``slugifyName`` faithfully, as later revised by CLAUDE.md's "SKU/serial-ID
format change, confirmed 2026-09-30".

Convention:

    {CATEGORY_PREFIX}-{first 1-2 words of the item name, each truncated to a
    maximum of 4 letters, slugified}-{4-digit sequence}

e.g. "Charizard VMAX Box" in TCG -> ``TCG-CHAR-VMAX-0001`` (was
``TCG-CHARIZARD-VMAX-0001`` before the 2026-09-30 change — "Charizard"
truncates to "CHAR", "VMAX" is already <=4 letters so is unchanged). Only the
name portion is truncated — category prefixes (``TCG``/``WATCH``/``AUTO``/
``TOY``/``OTH``) and the ``CONSIGN-`` tag are untouched by this rule (see
``inventory/consignment.py``).

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

This is a forward-only change — SKUs already generated under the old
"full word" convention are never retroactively renamed (none exist in real
production data as of the 2026-09-30 decision).
"""
from __future__ import annotations

import re
from typing import Iterable

_NON_ALNUM_RUN = re.compile(r"[^A-Z0-9]+")

_MAX_WORD_LETTERS = 4


def _clean_word(word: str) -> str:
    """Upper-cases a single word and collapses any run of non-alphanumeric
    characters within it to a single hyphen, stripping leading/trailing
    hyphens — the same collapsing rule ``slugify_name`` always applied,
    just scoped to one word at a time instead of the whole joined name (so
    truncation below happens on the cleaned-up slug, not the raw word).
    """
    return _NON_ALNUM_RUN.sub("-", (word or "").upper()).strip("-")


def slugify_name(name: str) -> str:
    """First 1-2 words of ``name``, each cleaned up (upper-cased,
    non-alphanumeric runs collapsed to a single hyphen, leading/trailing
    hyphens stripped) and then truncated to a maximum of 4 letters, joined
    with a hyphen. Falls back to "ITEM" for an empty/whitespace-only name
    (or a name whose first 1-2 words are entirely non-alphanumeric).
    """
    words = (name or "").strip().split()[:2]
    parts = []
    for word in words:
        cleaned = _clean_word(word)
        if not cleaned:
            continue
        truncated = cleaned[:_MAX_WORD_LETTERS].strip("-")
        if truncated:
            parts.append(truncated)
    slug = "-".join(parts)
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
