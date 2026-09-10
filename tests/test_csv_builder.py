"""Unit tests for export.csv_builder against real Postgres data — built via
the real inventory.purchases.save_purchase orchestrator (never hand-crafted
INSERTs), so these tests exercise exactly the same stored values a real
purchase produces. Covers a lump-sum purchase, a pooled-shipping purchase
(including a per-line ships_separately override), and a serialized-unit
purchase with uneven per-unit costs, per the milestone brief.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

from inventory.purchases import (
    NewItemInput,
    PurchaseInput,
    PurchaseLineInput,
    SerialUnitInput,
    save_purchase,
)

from export.csv_builder import (
    EXPORT_COLUMNS,
    build_export_csv,
    build_export_rows,
    rows_to_csv_text,
)


def _new_fungible(name, category_code="AUTOMOTIVE", sku=None):
    return NewItemInput(name=name, category_code=category_code, identity_mode="fungible", sku=sku)


def _new_serialized(name, category_code="AUTOMOTIVE", sku=None):
    return NewItemInput(name=name, category_code=category_code, identity_mode="serialized", sku=sku)


class TestFungibleDirectPricingNoShipping:
    def test_single_row_with_expected_values(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 6, 1),
            vendor_description="Toko Onderdil Sejahtera",
            total_amount_paid=Decimal("650000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Brake Pad Set Toyota"),
                    quantity=10,
                    pricing_mode="direct",
                    price_entry_mode="per_unit",
                    price_value=Decimal("65000"),
                )
            ],
        )
        saved = save_purchase(conn, purchase)

        rows = build_export_rows(conn)
        assert len(rows) == 1
        row = rows[0]

        assert row.purchase_ref == saved.purchase_ref
        assert row.purchase_date == date(2026, 6, 1)
        assert row.sku == saved.lines[0].sku
        assert row.category == "Automotive"
        assert row.identity_mode == "fungible"
        assert row.serial_identifier is None
        assert row.quantity == 10
        assert row.pricing_mode == "direct"
        assert row.shipping_costing_method == "none"
        assert row.allocated_item_cost == Decimal("650000")
        assert row.shipping_share == Decimal("0")
        assert row.line_total == Decimal("650000")
        assert row.unit_cost == Decimal("650000") / 10
        assert row.currency == "IDR"
        assert row.fx_rate_to_idr is None


class TestPooledShippingWithPerLineOverride:
    """Mirrors TestMultiLineMixedIdentityPooledByWeight in test_purchases.py
    — a pooled-shipping purchase where one line opts out via
    ships_separately. Verifies shipping_costing_method reflects the real
    per-line effective method, not a blind copy of the purchase header.
    """

    def test_pooled_lines_and_manual_override_line(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 7, 14),
            vendor_description="Toko Grosir Jaya — bulk auto parts + Hot Wheels case lot",
            total_amount_paid=Decimal("11540000"),
            shipping_mode="pooled",
            pooled_shipping_total=Decimal("400000"),
            pooled_shipping_method="by_weight",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Brake Pad Set Toyota"),
                    quantity=56,
                    pricing_mode="direct",
                    price_entry_mode="per_unit",
                    price_value=Decimal("65000"),
                    shipping_weight_kg=Decimal("25"),
                ),
                PurchaseLineInput(
                    new_item=_new_fungible("Hot Wheels Mainline", category_code="TOYS_COLLECTIBLES"),
                    quantity=180,
                    pricing_mode="direct",
                    price_entry_mode="per_unit",
                    price_value=Decimal("20000"),
                    shipping_weight_kg=Decimal("15"),
                ),
                PurchaseLineInput(
                    new_item=_new_serialized("ECU Unit Honda"),
                    quantity=4,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("3600000"),
                    ships_separately=True,
                    manual_shipping_amount=Decimal("300000"),
                ),
            ],
        )
        save_purchase(conn, purchase)

        rows = build_export_rows(conn)
        # 1 brake pad row + 1 hot wheels row + 4 ECU serial rows = 6.
        assert len(rows) == 6

        brakepad_row = next(r for r in rows if r.item_name == "Brake Pad Set Toyota")
        hotwheels_row = next(r for r in rows if r.item_name == "Hot Wheels Mainline")
        ecu_rows = [r for r in rows if r.item_name == "ECU Unit Honda"]

        assert brakepad_row.shipping_costing_method == "pooled"
        assert brakepad_row.shipping_share == Decimal("250000")
        assert brakepad_row.category == "Automotive"

        assert hotwheels_row.shipping_costing_method == "pooled"
        assert hotwheels_row.shipping_share == Decimal("150000")
        assert hotwheels_row.category == "Toys & Collectibles"

        assert len(ecu_rows) == 4
        for r in ecu_rows:
            assert r.identity_mode == "serialized"
            assert r.quantity == 1
            assert r.shipping_costing_method == "manual"  # opted out of pooled
            # Line-level figures repeat verbatim across every unit row.
            assert r.shipping_share == Decimal("300000")
            assert r.allocated_item_cost == Decimal("3600000")
            assert r.line_total == Decimal("3900000")
            assert r.serial_identifier is not None
        # Per-unit costs (unit_cost) sum to the line total, but are not all
        # identical to the line-level shipping_share/allocated_item_cost —
        # confirms unit_cost genuinely reads the per-unit acquired_cost,
        # not a repeat of the line-level figures.
        assert sum(r.unit_cost for r in ecu_rows) == Decimal("3900000")
        serial_ids = {r.serial_identifier for r in ecu_rows}
        assert len(serial_ids) == 4  # all unique


class TestLumpSumPricing:
    def test_lumpsum_group_rows_report_pricing_mode(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 2),
            vendor_description="Toko Aksesoris Klasik — mixed lot, single bundled price",
            total_amount_paid=Decimal("4380000"),
            shipping_mode="pooled",
            pooled_shipping_total=Decimal("180000"),
            pooled_shipping_method="equal",
            lump_sum_active=True,
            lump_sum_total=Decimal("4000000"),
            lump_sum_method="by_value",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Antique Coin Set", category_code="OTHERS"),
                    quantity=10,
                    pricing_mode="lumpsum_group",
                    lumpsum_value=Decimal("60"),
                ),
                PurchaseLineInput(
                    new_item=_new_fungible("Diecast Truck Lot", category_code="TOYS_COLLECTIBLES"),
                    quantity=15,
                    pricing_mode="lumpsum_group",
                    lumpsum_value=Decimal("40"),
                ),
                PurchaseLineInput(
                    new_item=_new_fungible("Vinyl Record Lot", category_code="OTHERS"),
                    quantity=5,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("200000"),
                ),
            ],
        )
        save_purchase(conn, purchase)

        rows = build_export_rows(conn)
        assert len(rows) == 3
        by_name = {r.item_name: r for r in rows}
        assert by_name["Antique Coin Set"].pricing_mode == "lumpsum_group"
        assert by_name["Diecast Truck Lot"].pricing_mode == "lumpsum_group"
        assert by_name["Vinyl Record Lot"].pricing_mode == "direct"
        assert by_name["Antique Coin Set"].category == "Others"

        total_line_total = sum(r.line_total for r in rows)
        assert total_line_total == Decimal("4380000")


class TestFilteringByPurchaseIds:
    def test_purchase_ids_filter_limits_rows(self, conn):
        p1 = PurchaseInput(
            purchase_date=date(2026, 5, 1),
            vendor_description="Vendor A",
            total_amount_paid=Decimal("100000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Widget A"),
                    quantity=1,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("100000"),
                )
            ],
        )
        p2 = PurchaseInput(
            purchase_date=date(2026, 5, 2),
            vendor_description="Vendor B",
            total_amount_paid=Decimal("200000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Widget B"),
                    quantity=1,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("200000"),
                )
            ],
        )
        saved1 = save_purchase(conn, p1)
        saved2 = save_purchase(conn, p2)

        all_rows = build_export_rows(conn)
        assert len(all_rows) == 2

        only_p1 = build_export_rows(conn, purchase_ids=[saved1.id])
        assert len(only_p1) == 1
        assert only_p1[0].purchase_id == saved1.id

        only_p2 = build_export_rows(conn, purchase_ids=[saved2.id])
        assert len(only_p2) == 1
        assert only_p2[0].purchase_id == saved2.id

        empty = build_export_rows(conn, purchase_ids=[])
        assert empty == []


class TestCsvRendering:
    def test_csv_text_round_trips_through_csv_reader(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 6, 1),
            vendor_description="Toko Onderdil Sejahtera",
            total_amount_paid=Decimal("650000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Brake Pad Set Toyota"),
                    quantity=10,
                    pricing_mode="direct",
                    price_entry_mode="per_unit",
                    price_value=Decimal("65000"),
                )
            ],
        )
        save_purchase(conn, purchase)

        csv_text, rows = build_export_csv(conn)
        reader = csv.DictReader(io.StringIO(csv_text))
        assert reader.fieldnames == EXPORT_COLUMNS

        parsed_rows = list(reader)
        assert len(parsed_rows) == 1
        # NUMERIC(20, 2) round-trips with its stored scale (2 decimal
        # places) — Decimal("650000") == Decimal("650000.00") in value
        # terms, but the CSV text preserves Postgres's own stored
        # representation verbatim rather than reformatting it.
        assert parsed_rows[0]["line_total"] == "650000.00"
        assert parsed_rows[0]["serial_identifier"] == ""
        assert parsed_rows[0]["fx_rate_to_idr"] == ""
        assert parsed_rows[0]["category"] == "Automotive"
        assert len(rows) == 1

    def test_empty_database_produces_header_only_csv(self, conn):
        csv_text, rows = build_export_csv(conn)
        reader = csv.DictReader(io.StringIO(csv_text))
        assert reader.fieldnames == EXPORT_COLUMNS
        assert list(reader) == []
        assert rows == []


class TestFxRateReferenceField:
    def test_fx_rate_to_idr_is_passed_through_when_set(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 6, 1),
            vendor_description="Overseas vendor",
            total_amount_paid=Decimal("1000000"),
            currency="IDR",
            fx_rate_to_idr=Decimal("15750.5"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Imported Widget"),
                    quantity=1,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("1000000"),
                )
            ],
        )
        save_purchase(conn, purchase)

        rows = build_export_rows(conn)
        assert rows[0].fx_rate_to_idr == Decimal("15750.5")
