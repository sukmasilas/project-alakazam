"""Integration tests for inventory/opening_inventory.py against a real
Postgres database. An opening-inventory entry is a real Purchase under the
hood (see that module's docstring) — these tests confirm the orchestration
layer produces the exact same on-hand-stock/cost-basis result a normal
Purchase Entry would, and that the vendor-description default behaves as
specified.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from inventory.exceptions import ValidationError
from inventory.opening_inventory import (
    DEFAULT_VENDOR_LABEL,
    OpeningInventoryInput,
    OpeningInventorySerialUnitInput,
    record_opening_inventory,
)
from inventory.purchases import NewItemInput, PurchaseInput, PurchaseLineInput, save_purchase
from inventory.queries import get_item_by_sku, get_item_stats, get_purchase_detail


def _new_fungible(name, category_code="OTHERS", sku=None):
    return NewItemInput(name=name, category_code=category_code, identity_mode="fungible", sku=sku)


def _new_serialized(name, category_code="WATCHES", sku=None):
    return NewItemInput(name=name, category_code=category_code, identity_mode="serialized", sku=sku)


class TestNewFungibleItem:
    def test_appears_in_on_hand_stock_with_exact_cost_basis(self, conn):
        result = record_opening_inventory(
            conn,
            OpeningInventoryInput(
                quantity=10,
                price_entry_mode="per_unit",
                price_value=Decimal("50000"),
                new_item=_new_fungible("Existing Brake Pads"),
            ),
        )
        assert len(result.lines) == 1
        line = result.lines[0]
        assert line.allocated_item_cost == 500000
        assert line.shipping_share == 0
        assert line.line_total == 500000

        stats = get_item_stats(conn, line.sku)
        assert stats.quantity == 10
        assert stats.cost_basis == Decimal(500000)

    def test_total_price_entry_mode_produces_exact_cost_basis(self, conn):
        result = record_opening_inventory(
            conn,
            OpeningInventoryInput(
                quantity=4,
                price_entry_mode="total",
                price_value=Decimal("120000"),
                new_item=_new_fungible("Existing Widgets"),
            ),
        )
        line = result.lines[0]
        assert line.allocated_item_cost == 120000

        stats = get_item_stats(conn, line.sku)
        assert stats.quantity == 4
        assert stats.cost_basis == Decimal(120000)

    def test_shows_up_in_purchase_history(self, conn):
        result = record_opening_inventory(
            conn,
            OpeningInventoryInput(
                quantity=2,
                price_entry_mode="total",
                price_value=Decimal("40000"),
                new_item=_new_fungible("History Visible Widget"),
            ),
        )
        detail = get_purchase_detail(conn, result.purchase_ref)
        assert detail is not None
        assert detail.vendor_description == DEFAULT_VENDOR_LABEL
        assert len(detail.lines) == 1
        assert detail.lines[0].sku == result.lines[0].sku


class TestNewSerializedItemMultipleUnits:
    def test_per_unit_costs_and_serial_ids_are_correct(self, conn):
        result = record_opening_inventory(
            conn,
            OpeningInventoryInput(
                quantity=3,
                price_entry_mode="total",
                price_value=Decimal("30000000"),
                new_item=_new_serialized("Existing Rolex Lot"),
            ),
        )
        line = result.lines[0]
        assert line.line_total == 30000000
        assert len(line.serial_ids) == 3
        assert len(set(line.serial_ids)) == 3  # genuinely distinct
        for serial_id in line.serial_ids:
            assert serial_id.startswith(line.sku)

        item = get_item_by_sku(conn, line.sku)
        assert item.identity_mode == "serialized"
        stats = get_item_stats(conn, line.sku)
        assert stats.quantity == 3
        assert stats.cost_basis == Decimal(30000000)

    def test_explicit_per_unit_costs_and_serial_ids_are_honored(self, conn):
        result = record_opening_inventory(
            conn,
            OpeningInventoryInput(
                quantity=2,
                price_entry_mode="total",
                price_value=Decimal("10000000"),
                new_item=_new_serialized("Existing Omega Lot"),
                serial_units=[
                    OpeningInventorySerialUnitInput(serial_id="MY-OMEGA-001", cost=Decimal("4000000")),
                    OpeningInventorySerialUnitInput(serial_id="MY-OMEGA-002", cost=Decimal("6000000")),
                ],
            ),
        )
        line = result.lines[0]
        assert line.serial_ids == ["MY-OMEGA-001", "MY-OMEGA-002"]

        item = get_item_by_sku(conn, line.sku)
        from inventory.queries import get_serialized_unit_rows

        rows = {r.serial_id: r for r in get_serialized_unit_rows(conn, item.id)}
        assert rows["MY-OMEGA-001"].cost == Decimal("4000000")
        assert rows["MY-OMEGA-002"].cost == Decimal("6000000")

    def test_mismatched_explicit_unit_costs_are_rejected(self, conn):
        # Same hard-block behavior save_purchase() already enforces for any
        # serialized line — never silently accepted (see inventory/purchases.py
        # module docstring's "deliberate strengthening beyond the mockup").
        with pytest.raises(ValidationError):
            record_opening_inventory(
                conn,
                OpeningInventoryInput(
                    quantity=2,
                    price_entry_mode="total",
                    price_value=Decimal("10000000"),
                    new_item=_new_serialized("Bad Cost Split Lot"),
                    serial_units=[
                        OpeningInventorySerialUnitInput(serial_id="BAD-001", cost=Decimal("1000000")),
                        OpeningInventorySerialUnitInput(serial_id="BAD-002", cost=Decimal("1000000")),
                    ],
                ),
            )


class TestExistingItemWeightedAveragePooling:
    def test_adds_to_existing_on_hand_stock_via_weighted_average(self, conn):
        # A normal Purchase establishes the item at Rp 10,000/unit for 10
        # units (cost basis Rp 100,000).
        first = save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 6, 1),
                vendor_description="Original stock-up",
                total_amount_paid=Decimal("100000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        new_item=_new_fungible("Pooled Widget"),
                        quantity=10,
                        pricing_mode="direct",
                        price_entry_mode="total",
                        price_value=Decimal("100000"),
                    )
                ],
            ),
        )
        sku = first.lines[0].sku

        # An opening-inventory entry for the SAME item, at a different
        # per-unit value (Rp 20,000/unit for 6 units, Rp 120,000 total) —
        # should pool via the exact same weighted-average logic any normal
        # second purchase would use. No opening-inventory-specific pooling
        # code exists; this is free from reusing save_purchase() unchanged.
        record_opening_inventory(
            conn,
            OpeningInventoryInput(
                quantity=6,
                price_entry_mode="total",
                price_value=Decimal("120000"),
                sku=sku,
            ),
        )

        stats = get_item_stats(conn, sku)
        assert stats.quantity == 16
        assert stats.cost_basis == Decimal(220000)  # 100,000 + 120,000, exactly


class TestVendorDescriptionDefault:
    def test_defaults_to_opening_inventory_label(self, conn):
        result = record_opening_inventory(
            conn,
            OpeningInventoryInput(
                quantity=1,
                price_entry_mode="total",
                price_value=Decimal("50000"),
                new_item=_new_fungible("Default Label Widget"),
            ),
        )
        detail = get_purchase_detail(conn, result.purchase_ref)
        assert detail.vendor_description == DEFAULT_VENDOR_LABEL

    def test_blank_vendor_description_also_falls_back_to_default(self, conn):
        result = record_opening_inventory(
            conn,
            OpeningInventoryInput(
                quantity=1,
                price_entry_mode="total",
                price_value=Decimal("50000"),
                new_item=_new_fungible("Blank Label Widget"),
                vendor_description="   ",
            ),
        )
        detail = get_purchase_detail(conn, result.purchase_ref)
        assert detail.vendor_description == DEFAULT_VENDOR_LABEL

    def test_override_sticks(self, conn):
        result = record_opening_inventory(
            conn,
            OpeningInventoryInput(
                quantity=1,
                price_entry_mode="total",
                price_value=Decimal("50000"),
                new_item=_new_fungible("Overridden Label Widget"),
                vendor_description="Counted during 2026-09-30 physical stock take",
            ),
        )
        detail = get_purchase_detail(conn, result.purchase_ref)
        assert detail.vendor_description == "Counted during 2026-09-30 physical stock take"


class TestValidation:
    def test_non_positive_quantity_rejected(self, conn):
        with pytest.raises(ValidationError):
            record_opening_inventory(
                conn,
                OpeningInventoryInput(
                    quantity=0,
                    price_entry_mode="total",
                    price_value=Decimal("50000"),
                    new_item=_new_fungible("Zero Qty Widget"),
                ),
            )

    def test_unknown_price_entry_mode_rejected(self, conn):
        with pytest.raises(ValidationError):
            record_opening_inventory(
                conn,
                OpeningInventoryInput(
                    quantity=1,
                    price_entry_mode="bogus",
                    price_value=Decimal("50000"),
                    new_item=_new_fungible("Bad Mode Widget"),
                ),
            )

    def test_sku_and_new_item_both_missing_rejected_by_save_purchase(self, conn):
        # No new opening-inventory-specific check for this — save_purchase()
        # itself already enforces "exactly one of sku / new_item must be
        # set", and this module deliberately never duplicates that logic.
        with pytest.raises(ValidationError):
            record_opening_inventory(
                conn,
                OpeningInventoryInput(quantity=1, price_entry_mode="total", price_value=Decimal("1000")),
            )
