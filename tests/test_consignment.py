"""Tests for inventory/consignment.py — Milestone 8 consignment tracking.

Runs against a real Postgres database (same reasoning as every other
integration test in this project — the fungible-only-for-consignment
invariant, the purchase-traceability consistency invariant, and the
unpaid->paid compare-and-swap are real database mechanisms,
migrations/006_add_consignment.sql, not just app-level Python checks).
"""
from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from inventory.consignment import (
    ConsignmentIntakeInput,
    create_consignor,
    get_consignor,
    get_reimbursement,
    intake_consigned_units,
    list_consignors,
    list_reimbursements,
    mark_reimbursement_paid,
    sell_consigned_unit,
)
from inventory.exceptions import (
    AlreadyDepletedError,
    ConsignorNotFoundError,
    DuplicateSerialError,
    DuplicateSkuError,
    ItemMismatchError,
    ItemNotConsignedError,
    ReimbursementNotFoundError,
    ReimbursementNotUnpaidError,
    SerialUnitNotFoundError,
    ValidationError,
)
from inventory.items import create_item
from inventory.queries import get_item_by_sku, get_item_stats


def _new_consignor(conn, name="Budi Santoso", contact_info="0812-1111-2222"):
    return create_consignor(conn, name=name, contact_info=contact_info)


def _new_plain_serialized_item(conn, name="Owned Rolex", category_code="WATCHES"):
    """An ordinary, OWNED (non-consigned) serialized item — NOT tagged to
    any consignor. Used to prove consignment-specific functions correctly
    reject a plain owned item.
    """
    return create_item(conn, name=name, category_code=category_code, identity_mode="serialized").sku


def _new_fungible_item(conn, name="Owned Widget", category_code="OTHERS"):
    return create_item(conn, name=name, category_code=category_code, identity_mode="fungible").sku


class TestConsignorCrud:
    def test_create_and_list_consignor(self, conn):
        c = create_consignor(conn, name="Budi Santoso", contact_info="0812-1111-2222")
        assert c.id is not None
        assert c.name == "Budi Santoso"
        assert c.contact_info == "0812-1111-2222"

        rows = list_consignors(conn)
        assert any(r.id == c.id for r in rows)

    def test_create_consignor_without_contact_info(self, conn):
        c = create_consignor(conn, name="Anonymous Consignor")
        assert c.contact_info is None

    def test_create_consignor_rejects_blank_name(self, conn):
        with pytest.raises(ValidationError):
            create_consignor(conn, name="   ")

    def test_get_consignor_returns_none_when_missing(self, conn):
        assert get_consignor(conn, 999999) is None

    def test_get_consignor_found(self, conn):
        c = _new_consignor(conn)
        found = get_consignor(conn, c.id)
        assert found.id == c.id
        assert found.name == c.name


class TestIntakeConsignedUnits:
    def test_intake_creates_new_item_with_consign_prefix_sku(self, conn):
        consignor = _new_consignor(conn)
        result = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id,
                new_item_name="Rolex Daytona",
                new_item_category_code="WATCHES",
                quantity=2,
            ),
        )
        assert result.sku.startswith("CONSIGN-WATCH-ROLEX-DAYTONA-")
        assert result.consignor_id == consignor.id
        assert len(result.serial_ids) == 2
        assert len(set(result.serial_ids)) == 2  # genuinely distinct

        item = get_item_by_sku(conn, result.sku)
        assert item.identity_mode == "serialized"
        assert item.consignor_id == consignor.id

        # Zero cost basis, but genuinely 2 on-hand units — not a bug.
        stats = get_item_stats(conn, result.sku)
        assert stats.quantity == 2
        assert stats.cost_basis == Decimal("0")

    def test_intake_zero_cost_units_are_real_not_a_bug(self, conn):
        """Every serial unit's acquired_cost is exactly 0 — the seller never
        bought it (see CLAUDE.md/module docstring).
        """
        consignor = _new_consignor(conn)
        result = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id,
                new_item_name="Consigned Widget",
                new_item_category_code="OTHERS",
                quantity=3,
            ),
        )
        rows = conn.execute(
            text("SELECT acquired_cost, purchase_line_item_id FROM serial_units WHERE serial_id = ANY(:ids)"),
            {"ids": result.serial_ids},
        ).mappings().all()
        assert len(rows) == 3
        for row in rows:
            assert Decimal(row["acquired_cost"]) == Decimal("0")
            assert row["purchase_line_item_id"] is None

    def test_intake_reuses_existing_consigned_item(self, conn):
        consignor = _new_consignor(conn)
        first = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id,
                new_item_name="Omega Speedmaster",
                new_item_category_code="WATCHES",
                quantity=1,
            ),
        )
        second = intake_consigned_units(
            conn, ConsignmentIntakeInput(consignor_id=consignor.id, sku=first.sku, quantity=2)
        )
        assert second.item_id == first.item_id
        assert second.sku == first.sku
        assert len(second.serial_ids) == 2
        assert not (set(first.serial_ids) & set(second.serial_ids))  # no collision

        stats = get_item_stats(conn, first.sku)
        assert stats.quantity == 3  # 1 + 2
        assert stats.cost_basis == Decimal("0")

    def test_intake_rejects_nonexistent_consignor(self, conn):
        with pytest.raises(ConsignorNotFoundError):
            intake_consigned_units(
                conn,
                ConsignmentIntakeInput(
                    consignor_id=999999, new_item_name="X", new_item_category_code="OTHERS", quantity=1
                ),
            )

    def test_intake_rejects_reusing_a_non_serialized_item(self, conn):
        """A plain fungible item's SKU can never be reused for consignment
        intake — serialized only (no fungible consignment in this
        milestone).
        """
        consignor = _new_consignor(conn)
        sku = _new_fungible_item(conn)
        with pytest.raises(ValidationError, match="serialized"):
            intake_consigned_units(conn, ConsignmentIntakeInput(consignor_id=consignor.id, sku=sku, quantity=1))

    def test_intake_rejects_reusing_an_item_not_tagged_to_any_consignor(self, conn):
        """A plain, OWNED serialized item — never tagged to a consignor —
        cannot be reused as if it were a consigned item.
        """
        consignor = _new_consignor(conn)
        sku = _new_plain_serialized_item(conn)
        with pytest.raises(ItemNotConsignedError):
            intake_consigned_units(conn, ConsignmentIntakeInput(consignor_id=consignor.id, sku=sku, quantity=1))

    def test_intake_rejects_reusing_an_item_tagged_to_a_different_consignor(self, conn):
        consignor_a = _new_consignor(conn, name="Consignor A")
        consignor_b = _new_consignor(conn, name="Consignor B")
        first = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor_a.id, new_item_name="Shared-Name Item",
                new_item_category_code="OTHERS", quantity=1,
            ),
        )
        with pytest.raises(ValidationError, match="different consignor"):
            intake_consigned_units(
                conn, ConsignmentIntakeInput(consignor_id=consignor_b.id, sku=first.sku, quantity=1)
            )

    def test_intake_requires_exactly_one_of_sku_or_new_item_name(self, conn):
        consignor = _new_consignor(conn)
        with pytest.raises(ValidationError):
            intake_consigned_units(conn, ConsignmentIntakeInput(consignor_id=consignor.id, quantity=1))
        with pytest.raises(ValidationError):
            intake_consigned_units(
                conn,
                ConsignmentIntakeInput(
                    consignor_id=consignor.id, sku="X-0001", new_item_name="Y",
                    new_item_category_code="OTHERS", quantity=1,
                ),
            )

    def test_intake_rejects_nonpositive_quantity(self, conn):
        consignor = _new_consignor(conn)
        with pytest.raises(ValidationError):
            intake_consigned_units(
                conn,
                ConsignmentIntakeInput(
                    consignor_id=consignor.id, new_item_name="X", new_item_category_code="OTHERS", quantity=0
                ),
            )

    def test_intake_rejects_duplicate_explicit_serial_ids(self, conn):
        consignor = _new_consignor(conn)
        with pytest.raises(DuplicateSerialError):
            intake_consigned_units(
                conn,
                ConsignmentIntakeInput(
                    consignor_id=consignor.id, new_item_name="X", new_item_category_code="OTHERS",
                    quantity=2, serial_ids=["DUPE-001", "DUPE-001"],
                ),
            )

    def test_intake_rejects_explicit_sku_already_in_use(self, conn):
        consignor = _new_consignor(conn)
        intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id, new_item_name="X", new_item_category_code="OTHERS",
                quantity=1, new_item_sku="CONSIGN-OTH-TAKEN-0001",
            ),
        )
        with pytest.raises(DuplicateSkuError):
            intake_consigned_units(
                conn,
                ConsignmentIntakeInput(
                    consignor_id=consignor.id, new_item_name="Y", new_item_category_code="OTHERS",
                    quantity=1, new_item_sku="CONSIGN-OTH-TAKEN-0001",
                ),
            )

    def test_db_level_item_consignor_requires_serialized_trigger_is_a_real_backstop(self, conn):
        """Bypasses the app-level check entirely with a raw INSERT — the
        real DB trigger (migrations/006_add_consignment.sql) must still
        reject a fungible item with a consignor_id set, same two-layer
        discipline as every other cross-table invariant in this project
        (e.g. tests/test_preorders.py's own fungible-only bypass test).
        """
        consignor = _new_consignor(conn)
        category_id = conn.execute(text("SELECT id FROM categories WHERE code = 'OTHERS'")).scalar_one()
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO items (sku, name, category_id, identity_mode, consignor_id) "
                        "VALUES ('BYPASS-0001', 'Bypass Item', :category_id, 'fungible', :consignor_id)"
                    ),
                    {"category_id": category_id, "consignor_id": consignor.id},
                )

    def test_db_level_serial_unit_consistency_trigger_rejects_consigned_unit_with_purchase_line(self, conn):
        """A consigned item's serial unit must NEVER be tied to a
        purchase_line_items row — bypassed at the app layer, the real DB
        trigger must still reject it.
        """
        consignor = _new_consignor(conn)
        result = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id, new_item_name="Trigger Test Item",
                new_item_category_code="OTHERS", quantity=1,
            ),
        )
        item_id = result.item_id
        # Need a real purchase_line_items row to reference — from an
        # unrelated ordinary purchase.
        from inventory.purchases import NewItemInput, PurchaseInput, PurchaseLineInput, save_purchase

        purchase = save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 9, 15),
                vendor_description="Unrelated purchase for a real line id",
                total_amount_paid=Decimal("100000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        new_item=NewItemInput(name="Unrelated Item", category_code="OTHERS", identity_mode="fungible"),
                        quantity=1, pricing_mode="direct", price_entry_mode="total", price_value=Decimal("100000"),
                    )
                ],
            ),
        )
        line_item_id = purchase.lines[0].purchase_line_item_id

        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO serial_units (item_id, serial_id, acquired_cost, purchase_line_item_id) "
                        "VALUES (:item_id, 'BYPASS-CONSIGN-001', 0, :line_item_id)"
                    ),
                    {"item_id": item_id, "line_item_id": line_item_id},
                )

    def test_db_level_serial_unit_consistency_trigger_rejects_owned_unit_without_purchase_line(self, conn):
        """An ordinary (non-consigned) serial unit must ALWAYS trace back
        to a real purchase_line_items row — bypassed at the app layer, the
        real DB trigger must still reject a NULL purchase_line_item_id.
        """
        sku = _new_plain_serialized_item(conn)
        item = get_item_by_sku(conn, sku)
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO serial_units (item_id, serial_id, acquired_cost, purchase_line_item_id) "
                        "VALUES (:item_id, 'BYPASS-OWNED-001', 0, NULL)"
                    ),
                    {"item_id": item.id},
                )

    def test_db_level_serial_unit_consistency_trigger_rejects_nonzero_cost_for_consigned_unit(self, conn):
        consignor = _new_consignor(conn)
        result = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id, new_item_name="Nonzero Cost Test",
                new_item_category_code="OTHERS", quantity=1,
            ),
        )
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO serial_units (item_id, serial_id, acquired_cost, purchase_line_item_id) "
                        "VALUES (:item_id, 'BYPASS-NONZERO-001', 50000, NULL)"
                    ),
                    {"item_id": result.item_id},
                )


class TestSellConsignedUnit:
    def test_sells_unit_at_zero_cost_and_creates_unpaid_reimbursement(self, conn):
        consignor = _new_consignor(conn)
        intake = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id, new_item_name="Sellable Watch",
                new_item_category_code="WATCHES", quantity=1,
            ),
        )
        serial_id = intake.serial_ids[0]

        result = sell_consigned_unit(conn, serial_id=serial_id, expected_sku=intake.sku, reference="eBay #999")

        assert result.status == "unpaid"
        assert result.consignor_id == consignor.id
        assert result.serial_id == serial_id
        # The real depletion's own cost is genuinely 0 — correct, not a bug.
        assert result.depletion.acquired_cost == Decimal("0")

        stats = get_item_stats(conn, intake.sku)
        assert stats.quantity == 0

        reimbursement = get_reimbursement(conn, result.reimbursement_id)
        assert reimbursement.status == "unpaid"
        assert reimbursement.consignor_id == consignor.id
        assert reimbursement.serial_id == serial_id
        assert reimbursement.reference == "eBay #999"

    def test_no_sale_price_or_revenue_field_anywhere(self, conn):
        """Same standing rule as every prior milestone — confirmed by
        introspection, not just by not writing one. Note: 'reimbursement'
        legitimately contains no 'price'/'revenue'/'amount' substring, so
        this genuinely proves the absence rather than accidentally matching
        the word itself.
        """
        consignor = _new_consignor(conn)
        intake = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id, new_item_name="No Price Field Item",
                new_item_category_code="OTHERS", quantity=1,
            ),
        )
        result = sell_consigned_unit(conn, serial_id=intake.serial_ids[0], expected_sku=intake.sku)
        for obj in (result, intake):
            fields = vars(obj).keys()
            assert not any("price" in f.lower() or "revenue" in f.lower() or "amount" in f.lower() for f in fields)

    def test_rejects_selling_a_unit_of_a_non_consigned_item(self, conn):
        """A plain, OWNED serialized item must go through
        inventory.depletions.deplete_serial_unit() directly, never this
        function (see webapp/api.py's single Mark-as-Sold entry point for
        how the routing decision is made).
        """
        from inventory.purchases import NewItemInput, PurchaseInput, PurchaseLineInput, save_purchase

        purchase = save_purchase(
            conn,
            PurchaseInput(
                purchase_date=date(2026, 9, 15),
                vendor_description="Owned stock purchase",
                total_amount_paid=Decimal("500000"),
                shipping_mode="none",
                lines=[
                    PurchaseLineInput(
                        new_item=NewItemInput(name="Owned Serialized Item", category_code="WATCHES", identity_mode="serialized"),
                        quantity=1, pricing_mode="direct", price_entry_mode="total", price_value=Decimal("500000"),
                    )
                ],
            ),
        )
        serial_id = purchase.lines[0].serial_ids[0]
        sku = purchase.lines[0].sku
        with pytest.raises(ItemNotConsignedError):
            sell_consigned_unit(conn, serial_id=serial_id, expected_sku=sku)

        # Nothing was touched — the unit is still genuinely on hand.
        stats = get_item_stats(conn, sku)
        assert stats.quantity == 1

    def test_rejects_nonexistent_serial_id(self, conn):
        consignor = _new_consignor(conn)
        with pytest.raises(SerialUnitNotFoundError):
            sell_consigned_unit(conn, serial_id="NOPE-DOES-NOT-EXIST-001")

    def test_rejects_already_sold_unit(self, conn):
        consignor = _new_consignor(conn)
        intake = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id, new_item_name="Already Sold Item",
                new_item_category_code="OTHERS", quantity=1,
            ),
        )
        serial_id = intake.serial_ids[0]
        sell_consigned_unit(conn, serial_id=serial_id, expected_sku=intake.sku)
        with pytest.raises(AlreadyDepletedError):
            sell_consigned_unit(conn, serial_id=serial_id, expected_sku=intake.sku)

    def test_rejects_wrong_expected_sku(self, conn):
        consignor = _new_consignor(conn)
        intake = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id, new_item_name="Mismatch Item",
                new_item_category_code="OTHERS", quantity=1,
            ),
        )
        with pytest.raises(ItemMismatchError):
            sell_consigned_unit(conn, serial_id=intake.serial_ids[0], expected_sku="WRONG-SKU-0001")


class TestMarkReimbursementPaid:
    def _fresh_unpaid_reimbursement(self, conn):
        consignor = _new_consignor(conn)
        intake = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id, new_item_name="Reimbursement Test Item",
                new_item_category_code="OTHERS", quantity=1,
            ),
        )
        result = sell_consigned_unit(conn, serial_id=intake.serial_ids[0], expected_sku=intake.sku)
        return result.reimbursement_id

    def test_marks_paid(self, conn):
        reimbursement_id = self._fresh_unpaid_reimbursement(conn)
        result = mark_reimbursement_paid(conn, reimbursement_id, paid_date=date(2026, 9, 15))
        assert result.status == "paid"
        assert result.paid_date == date(2026, 9, 15)

    def test_marks_paid_with_default_date(self, conn):
        reimbursement_id = self._fresh_unpaid_reimbursement(conn)
        result = mark_reimbursement_paid(conn, reimbursement_id)
        assert result.status == "paid"
        assert result.paid_date == date.today()

    def test_payment_reference_overwrites_reference(self, conn):
        reimbursement_id = self._fresh_unpaid_reimbursement(conn)
        result = mark_reimbursement_paid(conn, reimbursement_id, payment_reference="Paid via BCA transfer #123")
        assert result.reference == "Paid via BCA transfer #123"

    def test_omitting_payment_reference_leaves_existing_reference_untouched(self, conn):
        consignor = _new_consignor(conn)
        intake = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id, new_item_name="Reference Preserved Item",
                new_item_category_code="OTHERS", quantity=1,
            ),
        )
        sale = sell_consigned_unit(conn, serial_id=intake.serial_ids[0], expected_sku=intake.sku, reference="eBay #42")
        result = mark_reimbursement_paid(conn, sale.reimbursement_id)
        assert result.reference == "eBay #42"

    def test_double_mark_paid_rejected(self, conn):
        reimbursement_id = self._fresh_unpaid_reimbursement(conn)
        mark_reimbursement_paid(conn, reimbursement_id)
        with pytest.raises(ReimbursementNotUnpaidError):
            mark_reimbursement_paid(conn, reimbursement_id)

    def test_nonexistent_id_raises_not_found(self, conn):
        with pytest.raises(ReimbursementNotFoundError):
            mark_reimbursement_paid(conn, 999999)


class TestListReimbursements:
    def test_filters_by_consignor_and_status(self, conn):
        consignor_a = _new_consignor(conn, name="Filter Consignor A")
        consignor_b = _new_consignor(conn, name="Filter Consignor B")

        intake_a = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor_a.id, new_item_name="Filter Item A",
                new_item_category_code="OTHERS", quantity=1,
            ),
        )
        intake_b = intake_consigned_units(
            conn,
            ConsignmentIntakeInput(
                consignor_id=consignor_b.id, new_item_name="Filter Item B",
                new_item_category_code="OTHERS", quantity=1,
            ),
        )
        sale_a = sell_consigned_unit(conn, serial_id=intake_a.serial_ids[0], expected_sku=intake_a.sku)
        sell_consigned_unit(conn, serial_id=intake_b.serial_ids[0], expected_sku=intake_b.sku)
        mark_reimbursement_paid(conn, sale_a.reimbursement_id)

        assert len(list_reimbursements(conn)) == 2
        assert len(list_reimbursements(conn, consignor_id=consignor_a.id)) == 1
        assert len(list_reimbursements(conn, status="paid")) == 1
        assert len(list_reimbursements(conn, status="unpaid")) == 1
        assert len(list_reimbursements(conn, consignor_id=consignor_b.id, status="unpaid")) == 1
        assert len(list_reimbursements(conn, consignor_id=consignor_b.id, status="paid")) == 0


# --------------------------------------------------------------------- #
# Genuine (barrier-synchronized) concurrency — the ONE genuinely new
# invariant this milestone needs: the unpaid->paid compare-and-swap. Uses
# threading.Barrier (true simultaneity), NOT event-ordered staggering —
# same discipline established in tests/test_depletions.py and
# tests/test_preorders.py.
# --------------------------------------------------------------------- #


def _fresh_unpaid_reimbursement_id(engine):
    with engine.begin() as setup_conn:
        from inventory.seed import seed_categories

        seed_categories(setup_conn)
        consignor = create_consignor(setup_conn, name="Barrier Consignor")
        intake = intake_consigned_units(
            setup_conn,
            ConsignmentIntakeInput(
                consignor_id=consignor.id, new_item_name="Barrier Consignment Item",
                new_item_category_code="OTHERS", quantity=1,
            ),
        )
        sale = sell_consigned_unit(setup_conn, serial_id=intake.serial_ids[0], expected_sku=intake.sku)
    return sale.reimbursement_id


class TestMarkReimbursementPaidGenuineConcurrency:
    TRIALS = 25

    def test_two_simultaneous_mark_paid_attempts_only_one_wins(self, engine):
        """Two threads race to mark the SAME reimbursement paid at the
        exact same instant. Exactly one must succeed; the other must get a
        clean ReimbursementNotUnpaidError — never a crash, never a
        deadlock, never both succeeding.
        """
        unexpected = []
        for _ in range(self.TRIALS):
            reimbursement_id = _fresh_unpaid_reimbursement_id(engine)

            barrier = threading.Barrier(2, timeout=10)
            results: dict = {}

            def racer(name: str):
                conn = engine.connect()
                try:
                    barrier.wait()
                    try:
                        mark_reimbursement_paid(conn, reimbursement_id)
                        conn.commit()
                        results[name] = "success"
                    except ReimbursementNotUnpaidError:
                        conn.rollback()
                        results[name] = "rejected"
                    except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
                        conn.rollback()
                        results[name] = f"UNEXPECTED:{type(exc).__name__}:{exc}"
                finally:
                    conn.close()

            ta = threading.Thread(target=racer, args=("a",))
            tb = threading.Thread(target=racer, args=("b",))
            ta.start()
            tb.start()
            ta.join(timeout=10)
            tb.join(timeout=10)

            outcomes = sorted(results.values())
            if any(v.startswith("UNEXPECTED") for v in outcomes):
                unexpected.append(outcomes)
                continue
            assert outcomes == ["rejected", "success"], outcomes

        assert not unexpected, (
            f"{len(unexpected)}/{self.TRIALS} trials produced an unexpected outcome "
            f"(deadlock or other uncaught error) instead of a clean success/rejection pair: {unexpected}"
        )
