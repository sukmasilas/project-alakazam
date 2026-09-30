"""Integration tests for inventory/locations.py — shelving/location
feature, confirmed 2026-10-01. Runs against a real Postgres database (same
reasoning as every other integration test in this project — the ``name``
uniqueness constraint is a real DB-level unique index, see migrations/
007_add_locations.sql).

Covers every item-creation insertion point CLAUDE.md's brief identified
(save_purchase() — used by BOTH Purchase Entry and Opening Inventory,
intake_consigned_units(), and the standalone items.create_item()) plus the
"editable afterward from Item Detail" update path, and the "never required"
nullable behavior at every one of those entry points.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from inventory.consignment import ConsignmentIntakeInput, create_consignor, intake_consigned_units
from inventory.exceptions import DuplicateLocationNameError, ValidationError
from inventory.items import create_item
from inventory.locations import create_location, get_location, list_locations, set_item_location
from inventory.opening_inventory import OpeningInventoryInput, record_opening_inventory
from inventory.purchases import NewItemInput, PurchaseInput, PurchaseLineInput, save_purchase
from inventory.queries import get_item_by_sku


def _new_fungible(name, category_code="OTHERS", sku=None, location_id=None):
    return NewItemInput(
        name=name, category_code=category_code, identity_mode="fungible", sku=sku, location_id=location_id
    )


def _new_serialized(name, category_code="WATCHES", sku=None, location_id=None):
    return NewItemInput(
        name=name, category_code=category_code, identity_mode="serialized", sku=sku, location_id=location_id
    )


class TestLocationCrud:
    def test_create_and_list_location(self, conn):
        loc = create_location(conn, name="Shelf A-3")
        assert loc.id is not None
        assert loc.name == "Shelf A-3"

        rows = list_locations(conn)
        assert any(r.id == loc.id and r.name == "Shelf A-3" for r in rows)

    def test_create_location_rejects_blank_name(self, conn):
        with pytest.raises(ValidationError):
            create_location(conn, name="   ")

    def test_create_location_rejects_duplicate_name(self, conn):
        create_location(conn, name="Warehouse B")
        with pytest.raises(DuplicateLocationNameError):
            create_location(conn, name="Warehouse B")

    def test_get_location_returns_none_when_missing(self, conn):
        assert get_location(conn, 999999) is None

    def test_list_locations_empty_by_default(self, conn):
        assert list_locations(conn) == []


class TestPurchaseEntryNewItemWithLocation:
    """Purchase Entry's inline new-item box — save_purchase()'s own
    new-item INSERT.
    """

    def test_new_fungible_item_persists_location(self, conn):
        loc = create_location(conn, name="Shelf A-1")
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 1),
            vendor_description="Toko Uji",
            total_amount_paid=Decimal("100000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Widget", location_id=loc.id),
                    quantity=1,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("100000"),
                )
            ],
        )
        result = save_purchase(conn, purchase)
        sku = result.lines[0].sku

        item = get_item_by_sku(conn, sku)
        assert item.location_id == loc.id

    def test_new_serialized_item_persists_location(self, conn):
        loc = create_location(conn, name="Display Case 1")
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 1),
            vendor_description="Toko Uji",
            total_amount_paid=Decimal("500000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=_new_serialized("Rolex Submariner", location_id=loc.id),
                    quantity=1,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("500000"),
                )
            ],
        )
        result = save_purchase(conn, purchase)
        sku = result.lines[0].sku

        item = get_item_by_sku(conn, sku)
        assert item.location_id == loc.id

    def test_new_item_with_no_location_is_nullable_not_required(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 1),
            vendor_description="Toko Uji",
            total_amount_paid=Decimal("100000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Widget No Location"),
                    quantity=1,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("100000"),
                )
            ],
        )
        result = save_purchase(conn, purchase)
        item = get_item_by_sku(conn, result.lines[0].sku)
        assert item.location_id is None

    def test_unknown_location_id_raises_validation_error_not_fk_violation(self, conn):
        purchase = PurchaseInput(
            purchase_date=date(2026, 9, 1),
            vendor_description="Toko Uji",
            total_amount_paid=Decimal("100000"),
            shipping_mode="none",
            lines=[
                PurchaseLineInput(
                    new_item=_new_fungible("Widget Bad Location", location_id=999999),
                    quantity=1,
                    pricing_mode="direct",
                    price_entry_mode="total",
                    price_value=Decimal("100000"),
                )
            ],
        )
        with pytest.raises(ValidationError):
            save_purchase(conn, purchase)


class TestOpeningInventoryNewItemWithLocation:
    """Opening Inventory's own new-item flow — a thin orchestration layer
    over the SAME save_purchase() Purchase Entry uses, per
    inventory/opening_inventory.py's own module docstring.
    """

    def test_new_item_persists_location(self, conn):
        loc = create_location(conn, name="Back Room")
        result = record_opening_inventory(
            conn,
            OpeningInventoryInput(
                quantity=5,
                price_entry_mode="per_unit",
                price_value=Decimal("20000"),
                new_item=_new_fungible("Existing Widget", location_id=loc.id),
            ),
        )
        item = get_item_by_sku(conn, result.lines[0].sku)
        assert item.location_id == loc.id

    def test_new_item_with_no_location_is_nullable(self, conn):
        result = record_opening_inventory(
            conn,
            OpeningInventoryInput(
                quantity=5,
                price_entry_mode="per_unit",
                price_value=Decimal("20000"),
                new_item=_new_fungible("Existing Widget No Loc"),
            ),
        )
        item = get_item_by_sku(conn, result.lines[0].sku)
        assert item.location_id is None


class TestConsignmentIntakeNewItemWithLocation:
    """Consignment Intake creates a new item through its OWN, separate
    insert logic (inventory/consignment.py::intake_consigned_units) — NOT
    save_purchase(). This is the entry point most likely to have been
    missed, so it gets its own dedicated coverage rather than being assumed
    to work because Purchase Entry's path was tested.
    """

    def test_new_consigned_item_persists_location(self, conn):
        loc = create_location(conn, name="Consignment Rack 1")
        consignor = create_consignor(conn, name="Budi Santoso")
        result = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id,
                new_item_name="Consigned Rolex",
                new_item_category_code="WATCHES",
                new_item_location_id=loc.id,
                quantity=1,
            ),
        )
        item = get_item_by_sku(conn, result.sku)
        assert item.location_id == loc.id
        assert item.consignor_id == consignor.id

    def test_new_consigned_item_with_no_location_is_nullable(self, conn):
        consignor = create_consignor(conn, name="Budi Santoso")
        result = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id,
                new_item_name="Consigned Rolex No Loc",
                new_item_category_code="WATCHES",
                quantity=1,
            ),
        )
        item = get_item_by_sku(conn, result.sku)
        assert item.location_id is None

    def test_unknown_location_id_raises_validation_error(self, conn):
        consignor = create_consignor(conn, name="Budi Santoso")
        with pytest.raises(ValidationError):
            intake_consigned_units(
                conn,
                ConsignmentIntakeInput(
                    consignor_id=consignor.id,
                    new_item_name="Consigned Rolex Bad Loc",
                    new_item_category_code="WATCHES",
                    new_item_location_id=999999,
                    quantity=1,
                ),
            )


class TestStandaloneCreateItemWithLocation:
    """The Inventory screen's own "+ Add New Item" panel — a genuinely
    fourth, separate item-creation path (POST /api/items ->
    inventory.items.create_item()), distinct from all three above.
    """

    def test_new_item_persists_location(self, conn):
        loc = create_location(conn, name="Front Counter")
        item = create_item(conn, name="Standalone Widget", category_code="OTHERS", identity_mode="fungible", location_id=loc.id)
        fetched = get_item_by_sku(conn, item.sku)
        assert fetched.location_id == loc.id

    def test_new_item_with_no_location_is_nullable(self, conn):
        item = create_item(conn, name="Standalone Widget No Loc", category_code="OTHERS", identity_mode="fungible")
        fetched = get_item_by_sku(conn, item.sku)
        assert fetched.location_id is None

    def test_unknown_location_id_raises_validation_error(self, conn):
        with pytest.raises(ValidationError):
            create_item(
                conn, name="Standalone Widget Bad Loc", category_code="OTHERS",
                identity_mode="fungible", location_id=999999,
            )


class TestUpdateExistingItemLocation:
    """Item Detail's "change location" action — set_item_location()."""

    def test_updates_location_on_existing_item(self, conn):
        loc_a = create_location(conn, name="Shelf 1")
        loc_b = create_location(conn, name="Shelf 2")
        item = create_item(conn, name="Movable Widget", category_code="OTHERS", identity_mode="fungible", location_id=loc_a.id)

        set_item_location(conn, sku=item.sku, location_id=loc_b.id)
        fetched = get_item_by_sku(conn, item.sku)
        assert fetched.location_id == loc_b.id

    def test_updates_location_on_item_with_no_prior_location(self, conn):
        loc = create_location(conn, name="Newly Assigned Shelf")
        item = create_item(conn, name="Widget No Prior Loc", category_code="OTHERS", identity_mode="fungible")
        assert item.location_id is None

        set_item_location(conn, sku=item.sku, location_id=loc.id)
        fetched = get_item_by_sku(conn, item.sku)
        assert fetched.location_id == loc.id

    def test_clears_location_back_to_none(self, conn):
        loc = create_location(conn, name="Temporary Shelf")
        item = create_item(conn, name="Widget To Clear", category_code="OTHERS", identity_mode="fungible", location_id=loc.id)

        set_item_location(conn, sku=item.sku, location_id=None)
        fetched = get_item_by_sku(conn, item.sku)
        assert fetched.location_id is None

    def test_unknown_sku_raises_validation_error(self, conn):
        with pytest.raises(ValidationError):
            set_item_location(conn, sku="NOT-A-REAL-SKU", location_id=None)

    def test_unknown_location_id_raises_validation_error(self, conn):
        item = create_item(conn, name="Widget Bad New Loc", category_code="OTHERS", identity_mode="fungible")
        with pytest.raises(ValidationError):
            set_item_location(conn, sku=item.sku, location_id=999999)
