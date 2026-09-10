"""Item-cost and shipping-cost allocation math, plus the reconciliation
invariant — the real engine behind docs/design/mockup.html's
``computeAllocation`` / ``splitInteger`` JS.

All money math here is done in ``decimal.Decimal`` and returns whole-rupiah
``int`` values — never ``float`` (see CLAUDE.md's brief: "Use NUMERIC for
all money, never float"). Every rupiah amount in this system is a whole
number (no sen/cent subdivision in practice), so "whole rupiah" is the
natural unit for both storage and this module's return values.

Faithfully ports the mockup's algorithm rather than redesigning it:
- ``split_integer`` matches ``splitInteger`` exactly, including its
  remainder-to-largest-share tie-break rule (which happens to assign the
  remainder to the *first* line when all weights are equal — see
  ``split_integer``'s docstring, and design doc Open Question 3).
- ``compute_purchase_allocation`` matches ``computeAllocation`` exactly:
  item cost and shipping cost are computed as two fully independent passes
  over the purchase's lines, then combined into each line's total.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Sequence

Numeric = Decimal | int | float | str


def round_half_up(value: Numeric) -> int:
    """Rounds to the nearest whole rupiah, ties away from zero for
    positive values (matches JS ``Math.round``'s behavior for the
    non-negative amounts this system always deals with).
    """
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _to_decimal(value: Optional[Numeric]) -> Decimal:
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


def split_integer(total: Numeric, weights: Sequence[Optional[Numeric]]) -> list[int]:
    """Splits an integer ``total`` across ``weights`` proportionally,
    rounding each share to the nearest whole rupiah, then assigning any
    leftover rounding remainder to the share with the largest raw
    (pre-rounding) value — so the result always sums EXACTLY to ``total``.

    Falls back to an even split if every weight is zero/None/negative
    (negative weights are treated as zero, matching the mockup's
    ``Math.max(0, w || 0)``).

    Tie-break rule (deliberately ported, not just an implementation detail):
    when multiple raw values tie for the largest, the *first* one in
    ``weights`` wins — a strict "greater than" comparison never displaces
    an earlier index for an equal value. In the common case of an even
    split (every weight equal), every raw value ties, so the remainder
    always lands on the first line/unit. This matches design doc Open
    Question 3's assumption ("even split, remainder to the first unit").
    """
    n = len(weights)
    if n == 0:
        return []

    total_int = round_half_up(total)
    norm_weights = [max(Decimal(0), _to_decimal(w)) for w in weights]
    total_weight = sum(norm_weights)

    if total_weight > 0:
        raw = [Decimal(total_int) * w / total_weight for w in norm_weights]
    else:
        raw = [Decimal(total_int) / n for _ in range(n)]

    rounded = [round_half_up(r) for r in raw]
    diff = total_int - sum(rounded)
    if diff != 0:
        idx = 0
        max_val: Optional[Decimal] = None
        for i, r in enumerate(raw):
            if max_val is None or r > max_val:
                max_val = r
                idx = i
        rounded[idx] += diff
    return rounded


# --------------------------------------------------------------------- #
# Price-entry-mode conversion (per-unit <-> total)
# --------------------------------------------------------------------- #

def convert_price_value(price_value: Numeric, quantity: int, from_mode: str, to_mode: str) -> int:
    """Genuinely CONVERTS a price value across the per-unit/total toggle —
    never reinterprets the same raw number under a new meaning (this was a
    real bug in the mockup's first draft, fixed before Milestone 1 sign-off;
    see CLAUDE.md's brief and design doc line 74).

    A per-unit -> total -> per-unit round trip always returns the exact
    original value: total = round(per_unit * qty) is an exact multiple of
    qty (since per_unit is itself already a whole-rupiah integer), so
    dividing back by qty recovers it exactly, with no rounding error.
    The reverse direction (total -> per_unit) may not divide evenly — this
    mirrors the mockup's "≈ Per unit" display, an approximation, not a
    claim of exactness.
    """
    if from_mode not in ("per_unit", "total") or to_mode not in ("per_unit", "total"):
        raise ValueError(f"Unknown price entry mode(s): {from_mode!r} -> {to_mode!r}")
    if from_mode == to_mode:
        return round_half_up(price_value)
    if from_mode == "per_unit" and to_mode == "total":
        return round_half_up(_to_decimal(price_value) * quantity)
    # from_mode == "total" and to_mode == "per_unit"
    if quantity <= 0:
        return round_half_up(price_value)
    return round_half_up(_to_decimal(price_value) / quantity)


# --------------------------------------------------------------------- #
# Purchase-level allocation
# --------------------------------------------------------------------- #

@dataclass
class LineInput:
    """One purchase line's allocation inputs. ``key`` is any hashable,
    caller-supplied identifier for the line (e.g. a 0-based index, or a
    provisional line id) — used only to keep results keyed to the right
    line; it carries no other meaning.
    """

    key: object
    quantity: int
    pricing_mode: str  # 'direct' | 'lumpsum_group'
    price_entry_mode: Optional[str] = None  # 'per_unit' | 'total' — required if pricing_mode == 'direct'
    price_value: Optional[Numeric] = None  # required if pricing_mode == 'direct'
    lumpsum_weight_kg: Optional[Numeric] = None
    lumpsum_value: Optional[Numeric] = None
    ships_separately: bool = False
    manual_shipping_amount: Optional[Numeric] = None
    shipping_weight_kg: Optional[Numeric] = None


@dataclass
class PurchaseAllocationInput:
    total_amount_paid: Numeric
    shipping_mode: str  # 'none' | 'manual' | 'pooled'
    pooled_shipping_total: Optional[Numeric] = None
    pooled_shipping_method: Optional[str] = None  # 'equal' | 'by_weight' | 'by_value'
    lump_sum_active: bool = False
    lump_sum_total: Optional[Numeric] = None
    lump_sum_method: Optional[str] = None  # 'equal' | 'by_weight' | 'by_value'
    lines: list[LineInput] = field(default_factory=list)


@dataclass
class LineAllocation:
    key: object
    allocated_item_cost: int
    shipping_share: int
    line_total: int


@dataclass
class PurchaseAllocation:
    lines: list[LineAllocation]
    running_total: int
    total_amount_paid: int
    diff: int  # total_amount_paid - running_total; 0 means balanced
    balanced: bool

    def line_by_key(self, key: object) -> LineAllocation:
        for line in self.lines:
            if line.key == key:
                return line
        raise KeyError(key)


def compute_purchase_allocation(purchase: PurchaseAllocationInput) -> PurchaseAllocation:
    """Computes item cost, shipping share, and line total for every line in
    a purchase, then the purchase-level running total vs. Total Amount
    Paid. This is pure computation — no I/O, no DB — so it's cheap to unit
    test exhaustively and reused unchanged by inventory.purchases.save_purchase
    for its hard pre-save reconciliation check.
    """
    lines = purchase.lines

    # --- Item cost: direct entry is authoritative per line; lump-sum
    # lines share one group pool, split by the purchase's single lump-sum
    # method (design doc: exactly one lump-sum group per purchase). ---
    item_cost_by_key: dict[object, int] = {}
    lumpsum_lines = [l for l in lines if l.pricing_mode == "lumpsum_group"]
    direct_lines = [l for l in lines if l.pricing_mode != "lumpsum_group"]

    for l in direct_lines:
        value = _to_decimal(l.price_value)
        if l.price_entry_mode == "per_unit":
            item_cost_by_key[l.key] = round_half_up(value * l.quantity)
        else:  # 'total'
            item_cost_by_key[l.key] = round_half_up(value)

    if lumpsum_lines:
        method = purchase.lump_sum_method or "equal"
        amount = purchase.lump_sum_total or 0

        def lumpsum_weight(l: LineInput) -> Numeric:
            if method == "equal":
                return l.quantity or 0
            if method == "by_weight":
                return l.lumpsum_weight_kg or 0
            return l.lumpsum_value or 0

        shares = split_integer(amount, [lumpsum_weight(l) for l in lumpsum_lines])
        for l, share in zip(lumpsum_lines, shares):
            item_cost_by_key[l.key] = share

    # --- Shipping: fully independent of item pricing, except that
    # pooled "by_value" reuses each line's already-computed item cost as
    # its proportional weight (design doc's resolved interpretation, not a
    # separate input). ---
    shipping_share_by_key: dict[object, int] = {}
    if purchase.shipping_mode == "manual":
        for l in lines:
            shipping_share_by_key[l.key] = round_half_up(l.manual_shipping_amount or 0)
    elif purchase.shipping_mode == "pooled":
        separate_lines = [l for l in lines if l.ships_separately]
        pooled_lines = [l for l in lines if not l.ships_separately]
        for l in separate_lines:
            shipping_share_by_key[l.key] = round_half_up(l.manual_shipping_amount or 0)

        method = purchase.pooled_shipping_method or "equal"

        def shipping_weight(l: LineInput) -> Numeric:
            if method == "equal":
                return l.quantity or 0
            if method == "by_weight":
                return l.shipping_weight_kg or 0
            return item_cost_by_key.get(l.key, 0)  # 'by_value'

        shares = split_integer(
            purchase.pooled_shipping_total or 0, [shipping_weight(l) for l in pooled_lines]
        )
        for l, share in zip(pooled_lines, shares):
            shipping_share_by_key[l.key] = share
    else:  # 'none'
        for l in lines:
            shipping_share_by_key[l.key] = 0

    result_lines = []
    for l in lines:
        item_cost = item_cost_by_key.get(l.key, 0)
        shipping_share = shipping_share_by_key.get(l.key, 0)
        result_lines.append(
            LineAllocation(
                key=l.key,
                allocated_item_cost=item_cost,
                shipping_share=shipping_share,
                line_total=item_cost + shipping_share,
            )
        )

    running_total = sum(rl.line_total for rl in result_lines)
    total_paid = round_half_up(purchase.total_amount_paid)
    diff = total_paid - running_total

    return PurchaseAllocation(
        lines=result_lines,
        running_total=running_total,
        total_amount_paid=total_paid,
        diff=diff,
        balanced=diff == 0,
    )


def split_serial_unit_costs(line_total: int, quantity: int) -> list[int]:
    """Default per-unit cost split for a serialized line: an even split of
    the line's total, remainder to the first unit (design doc Open
    Question 3) — exactly ``split_integer(line_total, [1] * quantity)``.
    """
    return split_integer(line_total, [1] * quantity)
