"""Advisory-only title-match suggestions for the eBay sales review queue.

Purely a suggestion to speed up a human reviewer — never auto-applied, never
a gate on anything (see CLAUDE.md's Milestone 6 "no row may ever auto-post
... even a very high-confidence exact string match"). Deliberately built on
the stdlib only (``difflib`` + a simple tokenizer), no new third-party
fuzzy-matching dependency, to keep this milestone's dependency-scanning
surface unchanged.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(value: str) -> set[str]:
    return set(_TOKEN_RE.findall((value or "").lower()))


@dataclass
class MatchCandidate:
    sku: str
    name: str
    category_code: str
    identity_mode: str
    score: float


def _score(ebay_title: str, catalog_name: str) -> float:
    """A blend of two cheap, dependency-free signals:

    - token recall: what fraction of the (usually short) catalog item
      name's own words actually appear somewhere in the (usually much
      longer, noisier) eBay listing title — e.g. a listing title carries
      grading/edition/language noise a catalog entry never bothered to
      record, so scoring on "does the catalog name's own vocabulary show
      up in the title" is more forgiving than a symmetric overlap measure.
    - difflib's whole-string ratio, as a cheap approximation of overall
      similarity that also rewards word ORDER and substring proximity,
      which pure token overlap ignores entirely.
    """
    title_tokens = _tokenize(ebay_title)
    name_tokens = _tokenize(catalog_name)
    if not title_tokens or not name_tokens:
        return 0.0
    overlap = len(title_tokens & name_tokens)
    token_recall = overlap / len(name_tokens)
    seq_ratio = difflib.SequenceMatcher(None, ebay_title.lower(), catalog_name.lower()).ratio()
    return round(0.7 * token_recall + 0.3 * seq_ratio, 4)


def suggest_item_matches(
    conn: Connection, ebay_title: str, limit: int = 3, min_score: float = 0.35
) -> list[MatchCandidate]:
    """Ranked catalog candidates for one eBay ``Item title`` — empty list
    means "no suggestion", not an error; the review UI must handle that
    case (a human still has to pick manually, or mark the row skipped).
    """
    rows = conn.execute(
        text(
            """
            SELECT i.sku, i.name, i.identity_mode, c.code AS category_code
            FROM items i
            JOIN categories c ON c.id = i.category_id
            """
        )
    ).mappings().all()

    scored: list[MatchCandidate] = []
    for row in rows:
        score = _score(ebay_title, row["name"])
        if score >= min_score:
            scored.append(
                MatchCandidate(
                    sku=row["sku"],
                    name=row["name"],
                    category_code=row["category_code"],
                    identity_mode=row["identity_mode"],
                    score=score,
                )
            )
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:limit]
