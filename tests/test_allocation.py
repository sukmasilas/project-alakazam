"""Pure unit tests for inventory/allocation.py — no database needed."""
from __future__ import annotations

from decimal import Decimal

from inventory.allocation import (
    LineInput,
    PurchaseAllocationInput,
    compute_purchase_allocation,
    convert_price_value,
    round_half_up,
    split_integer,
    split_serial_unit_costs,
)


class TestSplitInteger:
    def test_equal_split_remainder_goes_to_first(self):
        # 100 split 3 ways: 33.33 each -> 33, 33, 33, remainder 1 -> first index.
        assert split_integer(100, [1, 1, 1]) == [34, 33, 33]

    def test_proportional_split_by_weight_sums_exactly(self):
        result = split_integer(400000, [25, 15])
        assert sum(result) == 400000
        assert result == [250000, 150000]

    def test_zero_total_weight_falls_back_to_even_split(self):
        result = split_integer(90, [0, 0, 0])
        assert result == [30, 30, 30]

    def test_negative_weights_treated_as_zero(self):
        result = split_integer(100, [-5, 10, 10])
        assert sum(result) == 100
        assert result[0] == 0  # negative weight contributes nothing

    def test_empty_weights_returns_empty(self):
        assert split_integer(100, []) == []

    def test_result_always_sums_exactly_to_total_even_with_rounding(self):
        # 7 does not divide evenly by 3 weights.
        result = split_integer(7, [1, 1, 1])
        assert sum(result) == 7


class TestSplitSerialUnitCosts:
    def test_even_split_remainder_to_first_unit(self):
        # Design doc Open Question 3: "even split, remainder to first unit".
        assert split_serial_unit_costs(100, 3) == [34, 33, 33]

    def test_evenly_divisible_line_total(self):
        assert split_serial_unit_costs(90000000, 2) == [45000000, 45000000]


class TestConvertPriceValue:
    def test_round_trip_per_unit_total_per_unit_is_exact(self):
        original = Decimal("14000")
        qty = 120
        total = convert_price_value(original, qty, "per_unit", "total")
        assert total == 1_680_000
        back = convert_price_value(total, qty, "total", "per_unit")
        assert back == 14000

    def test_round_trip_holds_even_when_not_evenly_divisible_going_forward(self):
        # per-unit -> total is always an exact multiple of qty (since
        # per-unit is itself a whole-rupiah integer), so total -> per-unit
        # always recovers it exactly, regardless of qty.
        original = Decimal("65000")
        qty = 56
        total = convert_price_value(original, qty, "per_unit", "total")
        assert total == 65000 * 56
        back = convert_price_value(total, qty, "total", "per_unit")
        assert back == 65000

    def test_same_mode_is_a_no_op(self):
        assert convert_price_value(Decimal("500"), 10, "per_unit", "per_unit") == 500

    def test_total_to_per_unit_rounds_when_not_evenly_divisible(self):
        # 100 / 3 = 33.33 -> rounds to 33 (an approximation, "≈" in the UI).
        assert convert_price_value(Decimal("100"), 3, "total", "per_unit") == 33


class TestRoundHalfUp:
    def test_rounds_half_up_for_positive_values(self):
        assert round_half_up(Decimal("2.5")) == 3
        assert round_half_up(Decimal("2.4")) == 2


def _direct_line(key, qty, entry_mode, value, **kwargs) -> LineInput:
    return LineInput(
        key=key, quantity=qty, pricing_mode="direct", price_entry_mode=entry_mode, price_value=value, **kwargs
    )


def _lumpsum_line(key, qty, **kwargs) -> LineInput:
    return LineInput(key=key, quantity=qty, pricing_mode="lumpsum_group", **kwargs)


class TestComputePurchaseAllocationShippingNone:
    def test_no_shipping_every_line_is_zero(self):
        purchase = PurchaseAllocationInput(
            total_amount_paid=1_500_000,
            shipping_mode="none",
            lines=[
                _direct_line("a", 10, "per_unit", Decimal("100000")),
                _direct_line("b", 1, "total", Decimal("500000")),
            ],
        )
        alloc = compute_purchase_allocation(purchase)
        assert alloc.balanced
        assert alloc.lines[0].shipping_share == 0
        assert alloc.lines[1].shipping_share == 0
        assert alloc.lines[0].allocated_item_cost == 1_000_000
        assert alloc.lines[1].allocated_item_cost == 500_000
        assert alloc.running_total == 1_500_000


class TestComputePurchaseAllocationShippingManual:
    def test_manual_shipping_uses_each_lines_own_amount(self):
        purchase = PurchaseAllocationInput(
            total_amount_paid=278_500_000,
            shipping_mode="manual",
            lines=[
                _direct_line("rolex", 2, "total", Decimal("210000000"), manual_shipping_amount=Decimal("1500000")),
                _direct_line("omega", 1, "total", Decimal("66500000"), manual_shipping_amount=Decimal("500000")),
            ],
        )
        alloc = compute_purchase_allocation(purchase)
        assert alloc.lines[0].shipping_share == 1_500_000
        assert alloc.lines[1].shipping_share == 500_000
        assert alloc.running_total == 210_000_000 + 1_500_000 + 66_500_000 + 500_000
        assert alloc.balanced

    def test_manual_shipping_defaults_missing_amount_to_zero(self):
        purchase = PurchaseAllocationInput(
            total_amount_paid=100_000,
            shipping_mode="manual",
            lines=[_direct_line("a", 1, "total", Decimal("100000"))],
        )
        alloc = compute_purchase_allocation(purchase)
        assert alloc.lines[0].shipping_share == 0
        assert alloc.balanced


class TestComputePurchaseAllocationShippingPooled:
    def test_pooled_equal_split_by_quantity(self):
        purchase = PurchaseAllocationInput(
            total_amount_paid=2_300_000,
            shipping_mode="pooled",
            pooled_shipping_total=Decimal("300000"),
            pooled_shipping_method="equal",
            lines=[
                _direct_line("a", 2, "total", Decimal("1000000")),
                _direct_line("b", 1, "total", Decimal("1000000")),
            ],
        )
        alloc = compute_purchase_allocation(purchase)
        # qty-weighted: 2 vs 1 -> 200000 / 100000
        assert alloc.lines[0].shipping_share == 200_000
        assert alloc.lines[1].shipping_share == 100_000
        assert alloc.balanced

    def test_pooled_by_weight_uses_per_line_weight_input(self):
        # Mirrors PUR-2026-0083 from the mockup sample data.
        purchase = PurchaseAllocationInput(
            total_amount_paid=11_540_000,
            shipping_mode="pooled",
            pooled_shipping_total=Decimal("400000"),
            pooled_shipping_method="by_weight",
            lines=[
                _direct_line("brakepad", 56, "per_unit", Decimal("65000"), shipping_weight_kg=Decimal("25")),
                _direct_line("hotwheels", 180, "per_unit", Decimal("20000"), shipping_weight_kg=Decimal("15")),
                _direct_line(
                    "ecu",
                    4,
                    "total",
                    Decimal("3600000"),
                    ships_separately=True,
                    manual_shipping_amount=Decimal("300000"),
                ),
            ],
        )
        alloc = compute_purchase_allocation(purchase)
        # ships_separately line is excluded from the weight pool entirely.
        ecu = alloc.line_by_key("ecu")
        assert ecu.shipping_share == 300_000
        # 25:15 split of 400000 -> 250000 / 150000
        assert alloc.line_by_key("brakepad").shipping_share == 250_000
        assert alloc.line_by_key("hotwheels").shipping_share == 150_000
        assert alloc.balanced
        assert alloc.running_total == 11_540_000

    def test_pooled_by_value_reuses_already_computed_item_cost_as_weight(self):
        purchase = PurchaseAllocationInput(
            total_amount_paid=1_100_000,
            shipping_mode="pooled",
            pooled_shipping_total=Decimal("100000"),
            pooled_shipping_method="by_value",
            lines=[
                _direct_line("cheap", 1, "total", Decimal("300000")),
                _direct_line("pricey", 1, "total", Decimal("700000")),
            ],
        )
        alloc = compute_purchase_allocation(purchase)
        # 300000:700000 -> 30000 / 70000
        assert alloc.line_by_key("cheap").shipping_share == 30_000
        assert alloc.line_by_key("pricey").shipping_share == 70_000
        assert alloc.balanced

    def test_ships_separately_line_excluded_from_pool(self):
        purchase = PurchaseAllocationInput(
            total_amount_paid=1_050_000,
            shipping_mode="pooled",
            pooled_shipping_total=Decimal("50000"),
            pooled_shipping_method="equal",
            lines=[
                _direct_line("a", 1, "total", Decimal("500000")),
                _direct_line(
                    "b", 1, "total", Decimal("500000"), ships_separately=True, manual_shipping_amount=Decimal("0")
                ),
            ],
        )
        alloc = compute_purchase_allocation(purchase)
        assert alloc.line_by_key("a").shipping_share == 50_000
        assert alloc.line_by_key("b").shipping_share == 0
        assert alloc.balanced


class TestComputePurchaseAllocationLumpSum:
    def test_lump_sum_mixed_with_direct_lines(self):
        # Mirrors PUR-2026-0102 from the mockup sample data: a lump-sum
        # group split By Value, pooled shipping split Equally, no direct
        # lines in this particular sample but exercised generally below.
        purchase = PurchaseAllocationInput(
            total_amount_paid=4_180_000,
            shipping_mode="pooled",
            pooled_shipping_total=Decimal("180000"),
            pooled_shipping_method="equal",
            lump_sum_active=True,
            lump_sum_total=Decimal("4000000"),
            lump_sum_method="by_value",
            lines=[
                _lumpsum_line("coins", 10, lumpsum_value=Decimal("60")),
                _lumpsum_line("trucks", 15, lumpsum_value=Decimal("40")),
            ],
        )
        alloc = compute_purchase_allocation(purchase)
        # 60:40 split of 4,000,000 -> 2,400,000 / 1,600,000
        assert alloc.line_by_key("coins").allocated_item_cost == 2_400_000
        assert alloc.line_by_key("trucks").allocated_item_cost == 1_600_000
        # equal (qty-weighted) split of 180,000 across qty 10 and 15 -> 25:15 ratio of 10:15
        assert alloc.line_by_key("coins").shipping_share == 72_000
        assert alloc.line_by_key("trucks").shipping_share == 108_000
        assert alloc.balanced
        assert alloc.running_total == 4_180_000

    def test_lump_sum_group_can_mix_with_directly_priced_lines(self):
        purchase = PurchaseAllocationInput(
            total_amount_paid=1_300_000,
            shipping_mode="none",
            lump_sum_active=True,
            lump_sum_total=Decimal("1000000"),
            lump_sum_method="equal",
            lines=[
                _lumpsum_line("in_bundle_a", 1),
                _lumpsum_line("in_bundle_b", 1),
                _direct_line("direct_one", 1, "total", Decimal("300000")),
            ],
        )
        alloc = compute_purchase_allocation(purchase)
        assert alloc.line_by_key("in_bundle_a").allocated_item_cost == 500_000
        assert alloc.line_by_key("in_bundle_b").allocated_item_cost == 500_000
        assert alloc.line_by_key("direct_one").allocated_item_cost == 300_000
        assert alloc.balanced


class TestReconciliation:
    def test_balanced_purchase_reports_balanced(self):
        purchase = PurchaseAllocationInput(
            total_amount_paid=1_000_000,
            shipping_mode="none",
            lines=[_direct_line("a", 1, "total", Decimal("1000000"))],
        )
        alloc = compute_purchase_allocation(purchase)
        assert alloc.balanced
        assert alloc.diff == 0

    def test_unbalanced_purchase_reports_the_signed_difference(self):
        purchase = PurchaseAllocationInput(
            total_amount_paid=1_000_000,
            shipping_mode="none",
            lines=[_direct_line("a", 1, "total", Decimal("900000"))],
        )
        alloc = compute_purchase_allocation(purchase)
        assert not alloc.balanced
        assert alloc.diff == 100_000
        assert alloc.running_total == 900_000
